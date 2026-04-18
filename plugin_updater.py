# ==========================================================
# [plugin_updater.py]
# ⚠️ 자가 업데이트 및 프로세스 제어 전용 플러그인 (수정본)
# 💡 깃허브 원격 저장소 강제 동기화 (git fetch & reset --hard)
# 💡 OS 독립적인 자가 재시작(os.execv) 및 데몬 롤백 제어 탑재
# 🚨 [수술 완료] systemctl 권한 문제 회피 및 메모리 100% 초기화 재시작 이식
# 🛡️ 업데이트 직전 stable_backup 폴더로 롤백용 안전띠 결속 기능 탑재
# ==========================================================
import logging
import asyncio
import subprocess
import os
import sys
from dotenv import load_dotenv

class SystemUpdater:
    def __init__(self):
        self.remote_branch = "origin/main"
        
        # 💡 [핵심 수술] .env 파일에서 사용자가 지정한 데몬 이름을 스캔, 없으면 'mybot'으로 폴백
        load_dotenv()
        self.daemon_name = os.getenv("DAEMON_NAME", "mybot")

    async def _create_safety_backup(self):
        """
        [롤백 봇(Rescue) 전용 아키텍처]
        업데이트를 시도한다는 것 = 현재 코드가 정상 작동 중이라는 뜻이므로,
        새로운 코드를 받기 전에 현재 파이썬 파일들을 stable_backup 폴더에 피신시킵니다.
        """
        try:
            backup_dir = "stable_backup"
            os.makedirs(backup_dir, exist_ok=True)
            
            # 현재 폴더의 모든 .py 파일들을 stable_backup 폴더로 복사 (에러 무시)
            proc = await asyncio.create_subprocess_shell(
                f"cp -p *.py {backup_dir}/ 2>/dev/null || true",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            await proc.communicate()
            logging.info("🛡️ [Updater] 롤백 봇을 위한 안전띠(stable_backup) 결속 완료")
        except Exception as e:
            logging.error(f"🚨 [Updater] 안전띠 결속 중 에러 발생 (업데이트는 계속 진행): {e}")

    async def pull_latest_code(self):
        """
        깃허브 서버와 통신하여 로컬의 변경 사항을 완벽히 무시하고
        원격 저장소의 최신 코드로 강제 덮어쓰기(Hard Reset)를 수행합니다.
        """
        # 💡 [안전띠 결속] 깃허브 동기화 직전에 현재 상태를 백업합니다!
        await self._create_safety_backup()

        try:
            fetch_proc = await asyncio.create_subprocess_shell(
                "git fetch --all",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            _, fetch_err = await fetch_proc.communicate()
            
            if fetch_proc.returncode != 0:
                error_msg = fetch_err.decode('utf-8').strip()
                logging.error(f"🚨 [Updater] Git Fetch 실패: {error_msg}")
                return False, f"Git Fetch 실패: {error_msg} (서버에서 git init 및 remote add 명령을 선행하십시오)"

            reset_proc = await asyncio.create_subprocess_shell(
                f"git reset --hard {self.remote_branch}",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            _, reset_err = await reset_proc.communicate()
            
            if reset_proc.returncode != 0:
                error_msg = reset_err.decode('utf-8').strip()
                logging.error(f"🚨 [Updater] Git Reset 실패: {error_msg}")
                return False, f"Git Reset 실패: {error_msg}"

            logging.info("✅ [Updater] 깃허브 최신 코드 강제 동기화 완료")
            return True, "깃허브 최신 코드가 로컬에 완벽히 동기화되었습니다."
            
        except Exception as e:
            logging.error(f"🚨 [Updater] 동기화 중 치명적 예외 발생: {e}")
            return False, f"업데이트 프로세스 예외 발생: {e}"

    def restart_daemon(self):
        """
        [수술 완료] 프로그램을 확실하게 끄고 새로운 코드로 다시 시작하는 마법의 주문입니다.
        """
        try:
            logging.info("🔄 [Updater] os.execv를 활용하여 현재 프로세스를 완전히 새로운 코드로 교체합니다.")
            
            # 파이썬(sys.executable)에게 현재 실행 중인 파일(sys.argv)을 "새로고침"해서 열라고 명령합니다.
            os.execv(sys.executable, [sys.executable] + sys.argv)
            return True
            
        except Exception as e:
            logging.error(f"🚨 [Updater] 자가 재시작 실패, 예비 플랜(systemctl)을 가동합니다: {e}")
            try:
                subprocess.Popen(
                    ["sudo", "systemctl", "restart", self.daemon_name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                return True
            except Exception as ex:
                logging.error(f"🚨 [Updater] 데몬 재가동 명령 하달 최종 실패: {ex}")
                return False
