from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import sqlite3
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks

from .menu import BLOG, KST, MenuError, MenuSource, week_start
from .schedule import ScheduleStore, is_due, next_run, parse_time


log = logging.getLogger(__name__)


class MenuTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id != self.client.guild_id:
            await interaction.response.send_message("이 봇은 지정된 서버에서만 사용할 수 있습니다.", ephemeral=True)
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        original = getattr(error, "original", error)
        if isinstance(original, app_commands.MissingPermissions):
            message = "자동 예약은 서버 관리 권한이 있는 사람만 변경할 수 있습니다."
        elif isinstance(original, (MenuError, ValueError)):
            message = str(original)
        else:
            log.error("명령 처리 실패", exc_info=(type(original), original, original.__traceback__))
            message = "요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class MenuBot(discord.Client):
    def __init__(self, guild_id: int, data_dir: Path):
        super().__init__(intents=discord.Intents.none(), allowed_mentions=discord.AllowedMentions.none())
        self.guild_id = guild_id
        self.store = ScheduleStore(data_dir / "bot.sqlite3")
        self.source = MenuSource(data_dir / "menu-cache")
        self.menu_lock = asyncio.Lock()
        self.delivery_lock = asyncio.Lock()
        self.tree = MenuTree(self)
        self.register_commands()

    async def setup_hook(self):
        guild = discord.Object(id=self.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self.deliver.start()

    async def on_ready(self):
        log.info("메뉴 봇 연결 완료: %s / 서버 %s", self.user, self.guild_id)

    async def close(self):
        self.deliver.cancel()
        task = self.deliver.get_task()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        self.store.close()
        await super().close()

    async def menu_message(self, day: dt.date, *, weekly: bool = False, refresh: bool = False):
        stamp = f"{day:%Y.%m.%d} ({'월화수목금토일'[day.weekday()]})"
        if not weekly and day.weekday() >= 5:
            return f"{stamp}은 주말 휴무입니다.\n출처: <{BLOG}>", None
        week = week_start(day)
        async with self.menu_lock:
            menu = await asyncio.to_thread(self.source.get_week, week, refresh=refresh)
        if menu is None:
            return f"{week:%Y.%m.%d}~{week + dt.timedelta(days=6):%Y.%m.%d} 주간메뉴표는 아직 게시되지 않았습니다.\n출처: <{BLOG}>", None
        if weekly:
            content = f"{week:%Y.%m.%d}~{week + dt.timedelta(days=6):%Y.%m.%d}\n{menu.title}"
            image = menu.full
        else:
            content = f"{stamp} Self 점심 메뉴"
            image = menu.days[day.weekday()] if menu.days else menu.full
            if image == menu.full:
                content += "\nSelf 영역을 확인하지 못해 주간 원본을 보여드립니다."
        return f"{content}\n출처: <{menu.url}>", discord.File(image, filename=f"menu-{day.isoformat()}{image.suffix}")

    async def show(self, interaction: discord.Interaction, day: dt.date, *, weekly: bool = False):
        await interaction.response.defer(thinking=True)
        content, attachment = await self.menu_message(day, weekly=weekly)
        try:
            await interaction.followup.send(content, **({"file": attachment} if attachment else {}))
        finally:
            if attachment:
                attachment.close()

    def register_commands(self):
        @self.tree.command(name="오늘점심", description="한국 시간 오늘의 Self 점심 메뉴를 보여줍니다.")
        async def today(interaction: discord.Interaction):
            await self.show(interaction, dt.datetime.now(KST).date())

        @self.tree.command(name="내일점심", description="한국 시간 다음 날짜의 Self 점심 메뉴를 보여줍니다.")
        async def tomorrow(interaction: discord.Interaction):
            await self.show(interaction, dt.datetime.now(KST).date() + dt.timedelta(days=1))

        @self.tree.command(name="이번주", description="일요일~토요일 기준 이번 주의 전체 메뉴표를 보여줍니다.")
        async def this_week(interaction: discord.Interaction):
            await self.show(interaction, dt.datetime.now(KST).date(), weekly=True)

        @self.tree.command(name="다음주", description="일요일~토요일 기준 다음 주의 전체 메뉴표를 보여줍니다.")
        async def next_week(interaction: discord.Interaction):
            await self.show(interaction, dt.datetime.now(KST).date() + dt.timedelta(days=7), weekly=True)

        @self.tree.command(name="오늘점심자동", description="평일 자동 발송을 조회·설정·해제합니다. 한국 시간 기준입니다.")
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        @app_commands.rename(clock="시간")
        @app_commands.describe(clock="HH:mm으로 설정, '해제'로 중지, 생략하면 현재 예약 조회")
        async def automatic(interaction: discord.Interaction, clock: str | None = None):
            await interaction.response.defer(ephemeral=True, thinking=True)
            async with self.delivery_lock:
                now = dt.datetime.now(KST)
                if clock is None:
                    schedule = self.store.get()
                    if schedule is None:
                        message = "등록된 자동 발송 예약이 없습니다."
                    else:
                        upcoming = next_run(schedule, now, self.store.consumed(now.date()))
                        message = f"평일 {schedule.clock} KST / <#{schedule.channel_id}>\n다음 예정: {upcoming:%Y.%m.%d %H:%M} KST"
                        if is_due(schedule, now) and not self.store.consumed(now.date()):
                            message = f"평일 {schedule.clock} KST / <#{schedule.channel_id}>\n오늘 발송을 기다리는 중입니다."
                elif clock.strip() == "해제":
                    self.store.disable()
                    message = "자동 발송을 해제했습니다."
                else:
                    clock = clock.strip()
                    parse_time(clock)
                    if interaction.channel is None or interaction.channel.type not in (discord.ChannelType.text, discord.ChannelType.news):
                        raise ValueError("자동 발송은 서버의 일반 텍스트 채널에서 설정해 주세요.")
                    permissions = interaction.app_permissions
                    if not (permissions.view_channel and permissions.send_messages and permissions.attach_files):
                        raise ValueError("이 채널에서 봇에 채널 보기·메시지 보내기·파일 첨부 권한을 부여해 주세요.")
                    schedule = self.store.set(interaction.channel_id, clock, now)
                    upcoming = next_run(schedule, now, self.store.consumed(now.date()))
                    message = (
                        f"평일 {clock} KST에 <#{schedule.channel_id}>로 자동 발송합니다.\n"
                        f"첫 예정: {upcoming:%Y.%m.%d %H:%M} KST\n"
                        "기존 예약은 이 시간과 채널로 변경했습니다. 날짜별 한 번만 발송합니다."
                    )
            await interaction.followup.send(message, ephemeral=True)

    @tasks.loop(seconds=30)
    async def deliver(self):
        try:
            async with self.delivery_lock:
                schedule = self.store.get()
                now = dt.datetime.now(KST)
                if schedule is None or not is_due(schedule, now) or self.store.consumed(now.date()):
                    return
                channel = await self.fetch_channel(schedule.channel_id)
                if not isinstance(channel, discord.TextChannel) or channel.guild.id != self.guild_id:
                    raise MenuError("예약된 발송 채널이 지정된 서버의 텍스트 채널이 아닙니다.")
                content, attachment = await self.menu_message(now.date(), refresh=True)
                try:
                    current = dt.datetime.now(KST)
                    if current.date() != now.date() or not is_due(schedule, current) or not self.store.claim(now.date()):
                        return
                    try:
                        message = await channel.send(
                            content, nonce=f"lunch-{now.date().isoformat()}",
                            **({"file": attachment} if attachment else {}),
                        )
                    except discord.HTTPException as exc:
                        if 400 <= exc.status < 500:
                            self.store.release(now.date())
                        raise
                    # shortcut: 전송 중 프로세스가 죽어 결과가 불명확하면 당일 재발송하지 않는다. 수신 확인이 필요해지면 복구 절차를 추가한다.
                    self.store.mark_sent(now.date(), message.id)
                    log.info("자동 발송 완료: %s", now.date())
                finally:
                    if attachment:
                        attachment.close()
        except (MenuError, discord.HTTPException, OSError, sqlite3.Error, TimeoutError):
            log.exception("자동 발송 실패: 허용된 10분 안에서만 재시도합니다. 전송 결과가 불명확한 예약은 재발송하지 않습니다.")

    @deliver.before_loop
    async def before_deliver(self):
        await self.wait_until_ready()


def main():
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    guild = os.environ.get("DISCORD_GUILD_ID", "").strip()
    if not token or not guild.isdigit() or not 0 < int(guild) < 2**64:
        raise SystemExit(".env에 DISCORD_TOKEN과 숫자로 된 DISCORD_GUILD_ID를 설정해 주세요.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    directory = os.environ.get("STATE_DIRECTORY") or os.environ.get("DATA_DIR", "data")
    client = MenuBot(int(guild), Path(directory).absolute())
    client.run(token, log_handler=None)
