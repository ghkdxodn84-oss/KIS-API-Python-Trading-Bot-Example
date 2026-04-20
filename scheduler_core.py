# ==========================================================
# [scheduler_core.py] - 🌟 100% 통합 완성본 (V27.26) 🌟
# ⚠️ 이 주석 및 파일명 표기는 절대 지우지 마세요.
# 💡 [V24.09 패치] API 결측치(None) 방어용 Safe Casting 전면 이식 완료
# 💡 [V24.10 수술] V_REV 동적 에스크로 차감 방어 (이중 차감 방지)
# 🚨 [V25.02 수술] 리버스 모드 일일 1회 확정 탈출 엔진 팩트 이식
# 🚨 [V27.12 그랜드 수술] 코파일럿 합작 - 리버스 하드스탑 부등호 논리 완벽 교정
# 🚨 [V27.13 그랜드 수술] 이벤트 루프 교착 방어 및 math.floor 평단가 왜곡 교정 완료
# 🚨 [V27.21 그랜드 수술] 5분 정산 윈도우 확장, TOCTOU 락온, 일반 종목 예산 누수 방어, Fail-Open 차단 및 Orphan 주문 초기화 보류(Skip) 이식 완비
# 🚀 [V27.24 그랜드 수술] 타임 패러독스 원천 차단! 08:30 동기화를 10:00 KST 확정 정산 시간으로 자동 시프트(Shift)하는 스마트 딜레이 엔진 탑재
# 🚀 [V27.25 그랜드 수술] 서머타임 데드락 해제, 잔고 조회 API 병목(O(N)->O(1)) 격상, 계절변경 Fail-Open 맹점 영구 적출
# 🛠️ [V27.26 긴급 패치] 17시 잔고 조회 시 증권사 API의 빈 리스트([]) 응답으로 인한 '.get' 에러(크래시) 원천 차단 방어막 이식
# ==========================================================
import os
import logging
import datetime
import pytz
import time
import math
import asyncio
import glob
import random
import pandas_market_calendars as mcal

def is_dst_active():
    est = pytz.timezone('US/Eastern')
    return datetime.datetime.now(est).dst() != datetime.timedelta(0)

def get_target_hour():
    return (17, "🌞 서머타임 적용(여름)") if is_dst_active() else (18, "❄️ 서머타임 해제(겨울)")

def is_market_open():
    try:
        est = pytz.timezone('US/Eastern')
        today = datetime.datetime.now(est)
        if today.weekday() >= 5: 
            return False
            
        nyse = mcal.get_calendar('NYSE')
        schedule = nyse.schedule(start_date=today.date(), end_date=today.date())
        
        if not schedule.empty:
            return True
        else:
            return False
    except Exception as e:
        logging.error(f"⚠️ 달력 라이브러리 에러 발생. 안전을 위해 강제 휴장 처리합니다: {e}")
        return False

def get_budget_allocation(cash, tickers, cfg):
    sorted_tickers = sorted(tickers, key=lambda x: 0 if x == "SOXL" else (1 if x == "TQQQ" else 2))
    allocated = {}
    
    safe_cash = float(cash) if cash is not None else 0.0
    
    dynamic_total_locked = 0.0
    for tx in tickers:
        rev_state = cfg.get_reverse_state(tx)
        if rev_state.get("is_active", False):
            is_locked = getattr(cfg, 'get_order_locked', lambda x: False)(tx)
            if not is_locked:
                dynamic_total_locked += float(cfg.get_escrow_cash(tx) or 0.0)

    free_cash = max(0.0, safe_cash - dynamic_total_locked)
    
    for tx in sorted_tickers:
        rev_state = cfg.get_reverse_state(tx)
        is_rev = rev_state.get("is_active", False)
        
        other_locked = dynamic_total_locked
        if is_rev:
            is_locked = getattr(cfg, 'get_order_locked', lambda x: False)(tx)
            if not is_locked:
                other_locked -= float(cfg.get_escrow_cash(tx) or 0.0)
        
        if is_rev:
            my_escrow = float(cfg.get_escrow_cash(tx) or 0.0)
            allocated[tx] = my_escrow + other_locked
        else:
            split = int(cfg.get_split_count(tx) or 0)
            seed = float(cfg.get_seed(tx) or 0.0)
            portion = seed / split if split > 0 else 0.0
            
            if free_cash >= portion:
                allocated[tx] = free_cash
                free_cash -= portion
            else: 
                allocated[tx] = 0.0
                
    return sorted_tickers, allocated

def get_actual_execution_price(execs, target_qty, side_cd):
    if not execs or target_qty <= 0: return 0.0
    
    execs.sort(key=lambda x: str(x.get('ord_tmd') or '000000'), reverse=True)
    matched_qty = 0
    total_amt = 0.0
    for ex in execs:
        if ex.get('sll_buy_dvsn_cd') == side_cd: 
            eqty = int(float(ex.get('ft_ccld_qty') or 0))
            eprice = float(ex.get('ft_ccld_unpr3') or 0.0)
            if matched_qty + eqty <= target_qty:
                total_amt += eqty * eprice
                matched_qty += eqty
            elif matched_qty < target_qty:
                rem = target_qty - matched_qty
                total_amt += rem * eprice
                matched_qty += rem
            
            if matched_qty >= target_qty:
                break
    
    if matched_qty > 0:
        return round(total_amt / matched_qty, 2)
    return 0.0

def perform_self_cleaning():
    try:
        now = time.time()
        seven_days = 7 * 24 * 3600
        one_day = 24 * 3600
        
        for f in glob.glob("logs/*.log"):
            if os.path.isfile(f) and os.stat(f).st_mtime < now - seven_days:
                try: os.remove(f)
                except: pass
                
        for f in glob.glob("data/*.bak_*"):
            if os.path.isfile(f) and os.stat(f).st_mtime < now - seven_days:
                try: os.remove(f)
                except: pass
                
        # NEW: [7일 초과 스냅샷 및 VWAP 상태 파일 소각 (디스크 부하 및 inode 누수 원천 차단)]
        for prefix in ["daily_snapshot_*", "vwap_state_*"]:
            for f in glob.glob(f"data/{prefix}.json"):
                if os.path.isfile(f) and os.stat(f).st_mtime < now - seven_days:
                    try: os.remove(f)
                    except: pass
                
        for directory in ["data", "logs"]:
            for f in glob.glob(f"{directory}/tmp*"):
                if os.path.isfile(f) and os.stat(f).st_mtime < now - one_day:
                    try: os.remove(f)
                    except: pass
    except Exception as e:
        logging.error(f"🧹 자정(Self-Cleaning) 작업 중 오류 발생: {e}")

async def scheduled_self_cleaning(context):
    await asyncio.to_thread(perform_self_cleaning)
    logging.info("🧹 [시스템 자정 작업 완료] 7일 초과 로그/백업 및 24시간 초과 임시 파일 소각 완료")

async def scheduled_token_check(context):
    jitter_seconds = random.randint(0, 180)
    logging.info(f"🔑 [API 토큰 갱신] 서버 동시 접속 부하 방지를 위해 {jitter_seconds}초 대기 후 발급을 시작합니다.")
    await asyncio.sleep(jitter_seconds)
    
    await asyncio.to_thread(context.job.data['broker']._get_access_token, force=True)
    logging.info("🔑 [API 토큰 갱신] 토큰 갱신이 안전하게 완료되었습니다.")

# ==========================================================
# 🚨 리버스 모드 절대 하드스탑(TQQQ -15% / SOXL -20%) 확정 탈출 엔진
# ==========================================================

async def scheduled_force_reset(context):
    kst = pytz.timezone('Asia/Seoul')
    now = datetime.datetime.now(kst)
    target_hour, _ = get_target_hour()
    
    now_minutes = now.hour * 60 + now.minute
    target_minutes = target_hour * 60
    
    diff = min((now_minutes - target_minutes) % 1440, (target_minutes - now_minutes) % 1440)
    
    if diff > 65:
        return
        
    if not is_market_open():
        await context.bot.send_message(chat_id=context.job.chat_id, text="⛔ <b>오늘은 미국 증시 휴장일입니다. 금일 시스템 매매 잠금 해제 및 정규장 주문 스케줄을 모두 건너뜁니다.</b>", parse_mode='HTML')
        return
    
    try:
        app_data = context.job.data
        cfg = app_data['cfg']
        broker = app_data['broker']
        tx_lock = app_data['tx_lock']
        chat_id = context.job.chat_id
        
        cfg.reset_locks()
        
        for t in cfg.get_active_tickers():
            if hasattr(cfg, 'set_order_locked'):
                cfg.set_order_locked(t, False)
        
        msg_addons = ""
        HARD_STOP_THRESHOLDS = {"TQQQ": -15.0, "SOXL": -20.0}
        
        async with tx_lock:
            _, holdings_snap = await asyncio.to_thread(broker.get_account_balance)
            
        # 🛠️ [V27.26 수술 핵심] 증권사 API가 빈 딕셔너리({}) 대신 빈 리스트([])를 던지는 고질적 버그를 완벽히 튕겨내는 타입 세이프 쉴드
        safe_holdings = holdings_snap if isinstance(holdings_snap, dict) else {}
            
        for t in cfg.get_active_tickers():
            rev_state = cfg.get_reverse_state(t)
            
            if rev_state.get("is_active"):
                async with tx_lock:
                    curr_p = await asyncio.to_thread(broker.get_current_price, t)
                
                # 에러 진원지 완벽 해결: 리스트([])에서는 get을 시도하지 않고 안전한 딕셔너리에서만 조회
                h_data = safe_holdings.get(t) or {}
                actual_avg = float(h_data.get('avg') or 0.0)
                curr_p = float(curr_p or 0.0)
                
                if curr_p > 0 and actual_avg > 0:
                    curr_ret = (curr_p - actual_avg) / actual_avg * 100.0
                    
                    exit_threshold = HARD_STOP_THRESHOLDS.get(t)
                    if exit_threshold is None:
                        logging.error(f"🚨 [FATAL] {t}에 대한 하드스탑 임계치가 설정되지 않았습니다.")
                        continue
                    
                    if curr_ret <= exit_threshold:
                        try:
                            cancelled = await asyncio.to_thread(broker.cancel_all_orders, t)
                            await asyncio.sleep(1.0)
                            logging.warning(f"🚨 [HardStop] {t} 미체결 주문 {cancelled}건 취소 완료")
                        except Exception as cancel_err:
                            logging.error(f"🚨 [HardStop] {t} 주문 취소 실패 — 수동 확인 필수: {cancel_err}")
                            await context.bot.send_message(chat_id=chat_id, text=f"🚨 <b>[{t}] 하드스탑 주문 취소 에러!</b> 미체결 주문을 수동으로 확인하세요. (상태 초기화 보류)", parse_mode='HTML')
                            continue 

                        cfg.set_reverse_state(t, False, 0, 0.0)
                        cfg.clear_escrow_cash(t)
                        
                        ledger_data = cfg.get_ledger()
                        changed = False
                        for lr in ledger_data:
                            if lr.get('ticker') == t and lr.get('is_reverse', False):
                                lr['is_reverse'] = False
                                changed = True
                        if changed:
                            cfg._save_json(cfg.FILES["LEDGER"], ledger_data)
                            
                        msg_addons += f"\n🚨 <b>[{t}] 하드스탑 확정 탈출 발동 (수익률: {curr_ret:.2f}% <= 기준: {exit_threshold}%)!</b>\n▫️ 격리 병동을 즉시 폐쇄하고 V14 본대로 완벽히 복귀했습니다."
                    else:
                        cfg.increment_reverse_day(t)
                else:
                    cfg.increment_reverse_day(t)
                
        final_msg = f"🔓 <b>[{target_hour}:00] 시스템 일일 초기화 완료 (매매 잠금 해제 & 팩트 스캔)</b>" + msg_addons
        await context.bot.send_message(chat_id=chat_id, text=final_msg, parse_mode='HTML')
        
    except Exception as e:
        await context.bot.send_message(chat_id=context.job.chat_id, text=f"🚨 <b>시스템 초기화 중 에러 발생:</b> {e}", parse_mode='HTML')

# ==========================================================
# 🚀 [V27.24] 스마트 딜레이 엔진: 모든 아침 정산을 KIS 배치 완료 시점인 10:00 KST로 강제 시프트
# ==========================================================

async def delayed_auto_sync(context):
    """10:00 KST에 최종 격발되는 실질적 정산 엔진"""
    await run_auto_sync(context, "10:00")

async def scheduled_auto_sync_summer(context):
    kst = pytz.timezone('Asia/Seoul')
    now = datetime.datetime.now(kst)
    
    if now.hour < 10:
        target_time = now.replace(hour=10, minute=0, second=0, microsecond=0)
        delay = (target_time - now).total_seconds()
        context.job_queue.run_once(delayed_auto_sync, delay, data=context.job.data, chat_id=context.job.chat_id)
        logging.info(f"⏳ [정산 지연 엔진 가동] 100% 확정 결제 데이터 스캔을 위해 동기화 스케줄을 10:00로 시프트합니다. ({delay}초 뒤 격발)")
        return
        
    await run_auto_sync(context, "10:00")

async def scheduled_auto_sync_winter(context):
    kst = pytz.timezone('Asia/Seoul')
    now = datetime.datetime.now(kst)
    
    if now.hour < 10:
        target_time = now.replace(hour=10, minute=0, second=0, microsecond=0)
        delay = (target_time - now).total_seconds()
        context.job_queue.run_once(delayed_auto_sync, delay, data=context.job.data, chat_id=context.job.chat_id)
        logging.info(f"⏳ [정산 지연 엔진 가동] 100% 확정 결제 데이터 스캔을 위해 동기화 스케줄을 10:00로 시프트합니다. ({delay}초 뒤 격발)")
        return
        
    await run_auto_sync(context, "10:00")

async def run_auto_sync(context, time_str):
    chat_id = context.job.chat_id
    bot = context.job.data['bot']
    status_msg = await context.bot.send_message(chat_id=chat_id, text=f"📝 <b>[{time_str}] 장부 자동 동기화 및 졸업 무결성 검증을 시작합니다.</b>", parse_mode='HTML')
    
    success_tickers = []
    for t in context.job.data['cfg'].get_active_tickers():
        res = await bot.process_auto_sync(t, chat_id, context, silent_ledger=True)
        if res == "SUCCESS":
            success_tickers.append(t)
            
    if success_tickers:
        async with context.job.data['tx_lock']:
            _, holdings = await asyncio.to_thread(context.job.data['broker'].get_account_balance)
        await bot._display_ledger(success_tickers[0], chat_id, context, message_obj=status_msg, pre_fetched_holdings=holdings)
    else:
        await status_msg.edit_text(f"📝 <b>[{time_str}] 장부 동기화 완료</b> (표시할 진행 중인 장부가 없습니다)", parse_mode='HTML')
