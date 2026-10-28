# 메가스터디 구내식당 메뉴 봇

메가푸드 블로그에서 해당 날짜의 주간메뉴표를 찾아 Discord에 표시합니다.
일별 메뉴는 날짜 행과 Self 영역을 자동으로 자릅니다. Take-Out은 포함하지 않습니다.
메뉴를 조회하거나 자동 발송할 때는 설명 문구 없이 이미지만 보냅니다.
모든 날짜와 예약 시간은 한국 표준시(KST) 기준입니다.

## 명령어

| 명령어 | 동작 |
| --- | --- |
| `/오늘점심`, `/오점` | 오늘 Self 메뉴. 주말에는 휴무 안내 |
| `/내일점심`, `/내점` | 다음 달력 날짜의 Self 메뉴. 금요일에는 토요일 휴무 안내 |
| `/이번주` | 일요일~토요일 기준 이번 주 전체 원본 |
| `/다음주` | 다음 일요일~토요일에 해당하는 전체 원본 |
| `/오늘점심자동 시간:11:30` | 현재 채널에 평일 11:30 자동 발송 예약 |
| `/오늘점심자동` | 현재 예약 조회 |
| `/오늘점심자동 시간:해제` | 예약 중지 |

한 서버에서 예약 하나만 유지합니다. 다시 설정하면 시간과 발송 채널을 함께 변경합니다.
메뉴는 모든 구성원이 조회할 수 있으며, 예약 명령은 **서버 관리** 권한이 필요합니다.
봇은 설정된 서버에만 명령을 등록하며 DM에서는 동작하지 않습니다.

주차는 일요일에 바뀝니다. 예를 들어 2026년 11월 1일 일요일의 `/이번주`는
11월 2~6일 메뉴를, `/다음주`는 11월 9~13일 메뉴를 찾습니다.

## Discord 설정 및 로컬 실행

1. [Discord Developer Portal](https://discord.com/developers/applications)에서 전용 애플리케이션과 봇을 만듭니다.
2. OAuth2 초대에 `bot`, `applications.commands`를 선택합니다. 봇 권한은 **채널 보기·메시지 보내기·파일 첨부**가 필요합니다. 관리자 권한이나 Message Content Intent는 필요 없습니다.
3. Discord 개발자 모드에서 서버 ID를 복사합니다.
4. `.env.example`을 `.env`로 복사해 `DISCORD_TOKEN`, `DISCORD_GUILD_ID`를 채웁니다. 토큰을 채팅이나 Git에 올리지 마세요.
5. Python 3.12와 [uv](https://docs.astral.sh/uv/)가 있는 환경에서 실행합니다.

```powershell
uv sync --locked
uv run --locked --env-file .env -m megastudy_menu_bot
```

`DATA_DIR`의 기본값은 작업 폴더의 `data`입니다. 이 폴더의 `bot.sqlite3`에 예약과 발송 기록을,
`menu-cache`에 주간 원본과 일별 이미지를 보관합니다. 봇을 끈 뒤 DB를 백업하면 예약을 함께 옮길 수 있습니다.

## 예약 동작

- 최초 실행 시 자동 발송은 꺼져 있습니다. Discord 명령으로 등록해야 발송합니다.
- 당일 누구든 `/오늘점심`이나 `/오점`으로 메뉴 이미지를 조회했다면 그날 자동 발송은 생략합니다. 조회 기록은 재시작 후에도 유지됩니다.
- `/내일점심`·`/내점`·주간 메뉴 조회와, 이미지가 없는 미게시 안내나 실패한 조회는 당일 자동 발송을 막지 않습니다.
- 오늘 미발송이고 입력 시각이 미래라면 오늘부터, 그 외에는 다음 평일부터 적용합니다.
- 30초 간격으로 발송 여부를 확인합니다. 주말은 생략하고 평일 공휴일은 메뉴표의 표시를 따릅니다.
- 장애나 재시작으로 시각을 놓치면 최대 10분 이내에서만 지연 발송합니다.
- 발송 기록은 예약 변경·해제·재시작 후에도 남으므로 날짜별 한 번만 자동 발송합니다.
- 전송 전에 SQLite에 날짜를 선점하고 Discord nonce 중복 방지도 사용합니다. 전송 중 종료되어 결과가 불명확하면 중복을 막기 위해 당일 재발송하지 않습니다. 이 경우 `/오늘점심`으로 직접 조회할 수 있습니다.

## 메뉴 수집과 크롭

출처는 [메가푸드 블로그](https://blog.naver.com/megafs01)입니다.
RSS에서 찾고 누락된 주는 메뉴 카테고리 목록으로 다시 조회합니다.
게시일 대신 **게시글 제목의 실제 적용 날짜**로 주차를 판별합니다.
네이버의 프레임 주소를 거치지 않고 게시물 본문에서 메뉴 이미지를 가져옵니다.

표 경계를 검출해 날짜 행과 Self 행만 자르며, Discord에서 읽기 쉽도록 폭이 작은 이미지는 확대합니다.
갈색 날짜 배경과 테두리를 구분하고, 휴무 그림 등 행이 합쳐진 셀은 주간 원본으로 대체합니다.
영역을 확신할 수 없을 때 임의 좌표로 잘라 보내지 않습니다.
메뉴표 미게시는 안내하고, 네트워크 오류는 미게시와 구분합니다. 다른 주의 메뉴를 대신 표시하지 않습니다.

일반 조회는 게시물 정보를 최대 15분 캐시하고, 자동 발송 직전에는 다시 확인합니다.
블로그 구조나 메뉴표 양식이 바뀌면 수집·크롭 기준을 조정해야 합니다.
식단표의 권리는 원 게시자에게 있습니다. 출처는 위의 메가푸드 블로그 링크에서 확인할 수 있습니다.

## OCI 운영

Python 3.12를 사용하며 외부에서 들어오는 포트를 열 필요가 없습니다.
`deploy/megastudy-menu-bot.service`는 전용 동적 사용자로 실행하고 재부팅 후 자동 시작합니다.
코드 위치는 `/opt/megastudy-menu-bot`, 환경 파일은 `/etc/megastudy-menu-bot.env`입니다.
환경 파일에는 위의 Discord 설정을 넣고 소유자는 root, 권한은 `600`으로 유지합니다.
systemd에서는 `STATE_DIRECTORY`를 우선 사용해 `/var/lib/megastudy-menu-bot`에 상태를 저장합니다.

서버에서 최초 설치할 때 프로젝트 폴더에 가상환경을 만든 뒤 의존성을 설치합니다.

```bash
cd /opt/megastudy-menu-bot
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock.txt
sudo install -m 644 deploy/megastudy-menu-bot.service /etc/systemd/system/megastudy-menu-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now megastudy-menu-bot
```

```bash
sudo systemctl status megastudy-menu-bot --no-pager
sudo journalctl -u megastudy-menu-bot -n 30 --no-pager
sudo systemctl restart megastudy-menu-bot
```

같은 토큰으로 로컬과 OCI 봇을 동시에 실행하지 마세요. 업데이트 시 상태 폴더는 보존합니다.
`uv.lock`은 로컬 환경용이고 `requirements.lock.txt`는 동일 버전을 서버의 pip에 전달하는 파일입니다.
의존성을 바꾸면 `uv lock` 후 다음 명령으로 함께 갱신합니다.

```bash
uv export --locked --no-dev --no-emit-project --format requirements-txt --output-file requirements.lock.txt
```

## 핵심 확인

```bash
uv run --locked -m megastudy_menu_bot.checks
```

외부 메시지를 보내지 않고 일~토 주차, 연말 날짜 범위, 시간 입력, 재설정과 재시작,
날짜별 중복 방지, 10분 지연, 색 배경이 있는 표의 Self 경계와 원본 대체를 확인합니다.

`Gitcatho/mega_lunch`는 수집·크롭 방식 참고 자료이며 저장소를 복제하거나 기능을 그대로 이식하지 않았습니다.
