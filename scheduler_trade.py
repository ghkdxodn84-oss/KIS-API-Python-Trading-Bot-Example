# ==========================================================
# [scheduler_trade.py] - Part 1/2 부 (상반부)
# ⚠️ 수술 내역: 
# 🚨 [V25.03 긴급 수술] 타임존 Glitch 방어 및 상대적 시간 윈도우(Relative Time Window) 도입
# 💡 하드코딩된 '15시' 비교를 폐기하고 '장 마감(market_close) 30분 전' 앵커 시스템으로 전면 교체
# 🚨 [V25.04 패치] 듀얼 레퍼런싱 데이터(SOXX/SOXL) 이원화 호출 및 파이프라인 연동
# ==========================================================
import os
import logging
import datetime
import pytz
import time
import math
import asyncio
import glob
import json
import pandas_market_calendars as mcal
import random

from scheduler_core import is_market_open, get_budget_allocation, get_target_hour

# ==========================================================
# 1. 🔫 스나이퍼 모니터링 (하이브리드 AVWAP 및 V14 상방 스나이퍼)
# ==========================================================
async def scheduled_sniper_monitor(context):
    if not is_market_open(): return
    
    est = pytz.timezone('US/Eastern')
    now_est = datetime.datetime.now(est)
    
    try:
        nyse = mcal.get_calendar('NYSE')
        schedule = nyse.schedule(start_date=now_est.date(), end_date=now_est.date())
        if schedule.empty: return
        
        market_open = schedule.iloc[0]['market_open'].astimezone(est)
        market_close = schedule.iloc[0]['market_close'].astimezone(est)
    except Exception:
        if now_est.weekday() < 5:
            market_open = now_est.replace(hour=9, minute=30, second=0, microsecond=0)
            market_close = now_est.replace(hour=16, minute=0, second=0, microsecond=0)
        else: return
    
    pre_start = market_open.replace(hour=4, minute=0)
    start_monitor = pre_start + datetime.timedelta(minutes=1)
    end_monitor = market_close - datetime.timedelta(minutes=1)
    
    if not (start_monitor <= now_est <= end_monitor):
        return

    is_regular_session = market_open <= now_est <= market_close
    
    app_data = context.job.data
    cfg, broker, strategy, tx_lock = app_data['cfg'], app_data['broker'], app_data['strategy'], app_data['tx_lock']
    base_map = app_data.get('base_map', {})  # NEW: 듀얼 레퍼런싱 매핑 로드
    chat_id = context.job.chat_id
    
    tracking_cache = app_data.setdefault('sniper_tracking', {})
    
    today_est_str = now_est.strftime('%Y%m%d')
    if tracking_cache.get('date') != today_est_str:
        tracking_cache.clear()
        tracking_cache['date'] = today_est_str
        try:
            for _f in glob.glob("data/sniper_cache_*.json"): os.remove(_f)
        except: pass
            
    async def _do_sniper():
        async with tx_lock:
            cash, holdings = broker.get_account_balance()
            if holdings is None: return
            avwap_free_cash = cash
            
            for t in cfg.get_active_tickers():
                version = cfg.get_version(t)
                
                if version == "V_REV":
                    if not cfg.get_avwap_hybrid_mode(t): continue
                    if tracking_cache.get(f"AVWAP_SHUTDOWN_{t}"): continue
                    
                    target_base = base_map.get(t, t)  # NEW: 기초자산 티커 추출 (ex. SOXX)
                    
                    if f"AVWAP_CTX_{t}" not in tracking_cache:
                        # MODIFIED: 기초자산 기준 매크로 컨텍스트 획득
                        ctx_data = await asyncio.to_thread(strategy.fetch_avwap_macro, target_base)
                        tracking_cache[f"AVWAP_CTX_{t}"] = ctx_data
                    
                    ctx_data = tracking_cache.get(f"AVWAP_CTX_{t}")
                    avwap_qty = tracking_cache.get(f"AVWAP_QTY_{t}", 0)
                    avwap_avg = tracking_cache.get(f"AVWAP_AVG_{t}", 0.0)
                    
                    # 💡 [핵심 수술] 파생상품(SOXL)과 기초자산(SOXX) 데이터 이원화 동시 호출
                    exec_curr_p = float(await asyncio.to_thread(broker.get_current_price, t) or 0.0)
                    if exec_curr_p <= 0: continue
                    
                    base_curr_p = float(await asyncio.to_thread(broker.get_current_price, target_base) or 0.0)
                    if base_curr_p <= 0: continue
                    
                    # 기초자산 시가 도출
                    base_day_open, _ = await asyncio.to_thread(broker.get_day_high_low, target_base)
                    base_day_open = float(base_day_open or 0.0)
                    
                    # 기초자산 1분봉 데이터 도출
                    df_1min_base = None
                    try: df_1min_base = await asyncio.to_thread(broker.get_1min_candles_df, target_base)
                    except: pass
                    
                    # MODIFIED: 이원화된 데이터셋을 플러그인에 주입
                    decision = strategy.get_avwap_decision(
                        target_base, t, base_curr_p, exec_curr_p, base_day_open, avwap_avg, avwap_qty, avwap_free_cash, ctx_data, df_1min_base, now_est
                    )
                    
                    action, reason = decision.get('action'), decision.get('reason')
                    
                    if action == 'SHUTDOWN':
                        tracking_cache[f"AVWAP_SHUTDOWN_{t}"] = True
                        await context.bot.send_message(chat_id=chat_id, text=f"🛑 <b>[{t}] 하이브리드 AVWAP 당일 작전 종료</b>\n▫️ 사유: {reason}", parse_mode='HTML')
                        
                    elif action == 'BUY' and not tracking_cache.get(f"AVWAP_BOUGHT_{t}"):
                        b_qty = decision.get('qty', 0)
                        if b_qty > 0:
                            # 주문 집행은 파생상품(SOXL) 호가창 타격
                            ask_p = float(await asyncio.to_thread(broker.get_ask_price, t) or exec_curr_p)
                            res = broker.send_order(t, "BUY", b_qty, ask_p, "LIMIT")
                            if res.get('rt_cd') == '0':
                                tracking_cache[f"AVWAP_BOUGHT_{t}"], tracking_cache[f"AVWAP_QTY_{t}"], tracking_cache[f"AVWAP_AVG_{t}"] = True, b_qty, ask_p
                                await context.bot.send_message(chat_id=chat_id, text=f"🎯 <b>[{t}] 하이브리드 AVWAP 딥매수 작렬!</b>\n▫️ 수량: {b_qty}주 / 단가: ${ask_p:.2f}", parse_mode='HTML')
                    
                    elif action == 'SELL' and avwap_qty > 0:
                        bid_p = float(await asyncio.to_thread(broker.get_bid_price, t) or exec_curr_p)
                        res = broker.send_order(t, "SELL", avwap_qty, bid_p, "LIMIT")
                        if res.get('rt_cd') == '0':
                            tracking_cache[f"AVWAP_SHUTDOWN_{t}"], tracking_cache[f"AVWAP_QTY_{t}"], tracking_cache[f"AVWAP_AVG_{t}"] = True, 0, 0.0
                            await context.bot.send_message(chat_id=chat_id, text=f"🏆 <b>[{t}] 하이브리드 AVWAP 독립물량 청산 완료!</b>", parse_mode='HTML')
                    continue

                # (이하 V14 상방 스나이퍼 로직 동일 유지...)
# ==========================================================
# [scheduler_trade.py] - Part 2/2 부 (하반부)
# 🚨 [V25.03 핵심 수술] 하드코딩 시간(Hour) 폐기 및 상대적 타임 윈도우(Relative Time Window) 이식
# 🚨 [V25.03 핵심 수술] 기존 LOC 덫 취소 실패 시 자가 치유(Self-Healing Nuke) 방어막 탑재
# ==========================================================

                # (상반부 V14 상방 스나이퍼 로직 종료 이후...)

# ==========================================================
# 2. 🛡️ Fail-Safe: 선제적 LOC 취소 (기본 스케줄러, 자가치유로 보완됨)
# ==========================================================
async def scheduled_vwap_init_and_cancel(context):
    if not is_market_open(): return
    
    app_data = context.job.data
    cfg, broker, tx_lock = app_data['cfg'], app_data['broker'], app_data['tx_lock']
    chat_id = context.job.chat_id
    
    vwap_cache = app_data.setdefault('vwap_cache', {})
    today_str = datetime.datetime.now(pytz.timezone('US/Eastern')).strftime('%Y%m%d')
    if vwap_cache.get('date') != today_str:
        vwap_cache.clear()
        vwap_cache['date'] = today_str
        
    async def _do_init():
        async with tx_lock:
            for t in cfg.get_active_tickers():
                if cfg.get_version(t) == "V_REV":
                    try:
                        await asyncio.to_thread(broker.cancel_all_orders_safe, t, "BUY")
                        await asyncio.to_thread(broker.cancel_all_orders_safe, t, "SELL")
                        vwap_cache[f"REV_{t}_nuked"] = True
                        
                        msg = f"🌅 <b>[{t}] 장 마감 30분 전 엔진 기상 (Fail-Safe 전환)</b>\n"
                        msg += f"▫️ 프리장에 선제 전송해둔 '예방적 양방향 LOC 덫'을 전량 취소(Nuke)합니다.\n"
                        msg += f"▫️ 1분 단위 정밀 타격(VWAP 슬라이싱) 모드로 교전 수칙을 변경합니다. ⚔️"
                        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML', disable_notification=True)
                        await asyncio.sleep(1.0)
                    except Exception as e:
                        logging.error(f"🚨 Fail-Safe 초기화(Nuke) 에러: {e}")
                        vwap_cache[f"REV_{t}_nuked"] = False 
                    
    try:
        await asyncio.wait_for(_do_init(), timeout=45.0)
    except Exception as e:
        logging.error(f"🚨 Fail-Safe 타임아웃 에러: {e}")

# ==========================================================
# 3. ⏱️ 1분봉 정밀 타격 (V-REV 전용 타임 슬라이싱)
# 🚨 [V25.03 수술] 시간 앵커(market_close) 기반 동적 역산 알고리즘 도입
# ==========================================================
async def scheduled_vwap_trade(context):
    if not is_market_open(): return
    
    est = pytz.timezone('US/Eastern')
    now_est = datetime.datetime.now(est)
    
    # 💡 [핵심 수술 1] 15시 30분 하드코딩 폐기 -> 장 마감 시간(market_close) 동적 획득
    try:
        nyse = mcal.get_calendar('NYSE')
        schedule = nyse.schedule(start_date=now_est.date(), end_date=now_est.date())
        if schedule.empty: return
        market_close = schedule.iloc[0]['market_close'].astimezone(est)
    except Exception:
        market_close = now_est.replace(hour=16, minute=0, second=0, microsecond=0)
        
    vwap_start_time = market_close - datetime.timedelta(minutes=30)
    vwap_end_time = market_close - datetime.timedelta(minutes=1)
    
    # 장 마감 30분 전 ~ 1분 전 사이에만 완벽하게 동작
    if not (vwap_start_time <= now_est <= vwap_end_time):
        return
        
    app_data = context.job.data
    cfg, broker, strategy, tx_lock = app_data['cfg'], app_data['broker'], app_data['strategy'], app_data['tx_lock']
    chat_id = context.job.chat_id
    
    vwap_cache = app_data.setdefault('vwap_cache', {})
    today_str = now_est.strftime('%Y%m%d')
    
    if vwap_cache.get('date') != today_str:
        vwap_cache.clear()
        vwap_cache['date'] = today_str

    U_CURVE_WEIGHTS = [
        0.0308, 0.0220, 0.0190, 0.0228, 0.0179, 0.0191, 0.0199, 0.0190, 0.0187, 0.0213,
        0.0216, 0.0234, 0.0231, 0.0210, 0.0205, 0.0252, 0.0225, 0.0228, 0.0238, 0.0229,
        0.0259, 0.0284, 0.0331, 0.0385, 0.0400, 0.0461, 0.0553, 0.0620, 0.0750, 0.1180
    ]
    
    # 💡 [핵심 수술 2] 장 마감까지 남은 분(minute)을 역산하여 U-Curve 인덱스 매핑
    minutes_to_close = int(max(1, (market_close - now_est).total_seconds()) / 60)
    min_idx = 30 - minutes_to_close
    if min_idx < 0: min_idx = 0
    if min_idx > 29: min_idx = 29
    current_weight = U_CURVE_WEIGHTS[min_idx]
        
    async def _do_vwap():
        async with tx_lock:
            cash, holdings = broker.get_account_balance()
            if holdings is None: return
            
            for t in cfg.get_active_tickers():
                if cfg.get_version(t) == "V_REV":
                    
                    # 💡 [핵심 수술 3] 자가 치유(Self-Healing Nuke) 방어막 가동
                    # 취소 스케줄러가 불발되었더라도 여기서 즉시 팩트 스캔 후 기존 덫 강제 파괴!
                    if not vwap_cache.get(f"REV_{t}_nuked"):
                        try:
                            await asyncio.to_thread(broker.cancel_all_orders_safe, t, "BUY")
                            await asyncio.to_thread(broker.cancel_all_orders_safe, t, "SELL")
                            vwap_cache[f"REV_{t}_nuked"] = True
                            msg = f"🌅 <b>[{t}] 하이브리드 타임 슬라이싱 기상 (자가 치유 가동)</b>\n"
                            msg += f"▫️ 장 마감 30분 전 진입을 확인하여 기존 LOC 덫을 강제 취소(Nuke)했습니다.\n"
                            msg += f"▫️ 스케줄러 누락을 완벽히 극복하고 1분 단위 정밀 타격을 즉각 개시합니다. ⚔️"
                            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML', disable_notification=True)
                            await asyncio.sleep(1.0)
                        except Exception as e:
                            logging.error(f"🚨 자가 치유 Nuke 실패: {e}")
                            continue # 취소에 실패하면 핑퐁 방지를 위해 다음 1분에 재시도
                            
                    strategy_rev = app_data.get('strategy_rev')
                    queue_ledger = app_data.get('queue_ledger')
                    if not strategy_rev or not queue_ledger: continue
                    
                    curr_p = float(await asyncio.to_thread(broker.get_current_price, t) or 0.0)
                    prev_c = float(await asyncio.to_thread(broker.get_previous_close, t) or 0.0)
                    if curr_p <= 0 or prev_c <= 0: continue
                    
                    q_data = queue_ledger.get_queue(t)
                    total_q = sum(item.get("qty", 0) for item in q_data)
                    avg_price = (sum(item.get("qty", 0) * item.get("price", 0.0) for item in q_data) / total_q) if total_q > 0 else 0.0
                    jackpot_trigger = avg_price * 1.010
                    
                    dates_in_queue = sorted(list(set(item.get('date') for item in q_data if item.get('date'))), reverse=True)
                    layer_1_qty = 0
                    layer_1_trigger = round(prev_c * 1.006, 2)
                    if dates_in_queue:
                        lots_for_date = [item for item in q_data if item.get('date') == dates_in_queue[0]]
                        layer_1_qty = sum(item.get('qty', 0) for item in lots_for_date)
                        if layer_1_qty > 0:
                            layer_1_price = sum(item.get('qty', 0) * item.get('price', 0.0) for item in lots_for_date) / layer_1_qty
                            layer_1_trigger = round(layer_1_price * 1.006, 2)
                    
                    # 💡 [핵심 수술 4] 스윕 피니셔 발동 시간을 "장 마감 2분 이하"로 동적 처리
                    if minutes_to_close <= 2 and not vwap_cache.get(f"REV_{t}_sweep_finished"):
                        target_sweep_qty = 0
                        sweep_type = ""
                        
                        if total_q > 0 and curr_p >= jackpot_trigger:
                            target_sweep_qty = total_q
                            sweep_type = "잭팟 전량"
                        elif layer_1_qty > 0 and curr_p >= layer_1_trigger:
                            target_sweep_qty = layer_1_qty
                            sweep_type = "1층 잔여물량"
                            
                        if target_sweep_qty > 0:
                            vwap_cache[f"REV_{t}_sweep_finished"] = True
                            
                            await asyncio.to_thread(broker.cancel_all_orders_safe, t, "SELL")
                            await asyncio.sleep(0.5)
                            
                            bid_price = float(await asyncio.to_thread(broker.get_bid_price, t) or 0.0)
                            exec_price = bid_price if bid_price > 0 else curr_p
                            
                            res = broker.send_order(t, "SELL", target_sweep_qty, exec_price, "LIMIT")
                            odno = res.get('odno', '')
                            
                            if res.get('rt_cd') == '0' and odno:
                                msg = f"🌪️ <b>[{t}] V-REV 본대 {sweep_type} 강제 청산 (Sweep Finisher) 발동!</b>\n"
                                if sweep_type == "잭팟 전량":
                                    msg += f"▫️ 장 마감을 2분 앞두고 잭팟 커트라인({jackpot_trigger:.2f}) 돌파를 확인했습니다.\n"
                                else:
                                    msg += f"▫️ 장 마감을 2분 앞두고 1층 앵커({layer_1_trigger:.2f}) 방어를 확인했습니다.\n"
                                msg += f"▫️ 미체결 잔량 <b>{target_sweep_qty}주</b>를 시장 매수호가(${exec_price:.2f})로 전량 폭격하여 지층을 완벽하게 소각합니다! 🏆"
                                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')
                                
                                ccld_qty = 0
                                for _ in range(4):
                                    await asyncio.sleep(2.0)
                                    execs = await asyncio.to_thread(broker.get_execution_history, t, today_str, today_str)
                                    my_execs = [ex for ex in execs if ex.get('odno') == odno]
                                    if my_execs:
                                        ccld_qty = sum(int(float(ex.get('ft_ccld_qty') or 0)) for ex in my_execs)
                                        if ccld_qty >= target_sweep_qty: break
                                        
                                if ccld_qty > 0:
                                    strategy_rev.record_execution(t, "SELL", ccld_qty, exec_price)
                                    queue_ledger.pop_lots(t, ccld_qty)
                        
                        if target_sweep_qty > 0 or (total_q > 0 and curr_p >= jackpot_trigger):
                            continue 
                    
                    try:
                        df_1min = await asyncio.to_thread(broker.get_1min_candles_df, t)
                        vwap_status = strategy.analyze_vwap_dominance(df_1min)
                    except Exception:
                        vwap_status = {"vwap_price": 0.0, "is_strong_up": False, "is_strong_down": False}
                    
                    current_regime = "SELL" if curr_p > prev_c else "BUY"
                    last_regime = vwap_cache.get(f"REV_{t}_regime")
                    
                    if last_regime and last_regime != current_regime:
                        await context.bot.send_message(
                            chat_id=chat_id, 
                            text=f"🔄 <b>[{t}] 실시간 공수 교대 발동!</b>\n"
                                 f"▫️ <b>[{last_regime} ➡️ {current_regime}]</b> 모드로 두뇌를 전환하며 궤도를 수정합니다.", 
                            parse_mode='HTML', disable_notification=True
                        )
                        try:
                            await asyncio.to_thread(broker.cancel_all_orders_safe, t, "BUY")
                            await asyncio.to_thread(broker.cancel_all_orders_safe, t, "SELL")
                            strategy_rev.reset_residual(t) 
                        except Exception as e:
                            err_msg = f"🛑 <b>[FATAL ERROR] {t} 공수 교대 중 기존 덫 취소 실패!</b>\n▫️ 2중 예산 소진 방어를 위해 당일 남은 V-REV 교전을 강제 중단(Hard-Lock)합니다.\n▫️ 상세 오류: {e}"
                            await context.bot.send_message(chat_id=chat_id, text=err_msg, parse_mode='HTML')
                            continue
                            
                    vwap_cache[f"REV_{t}_regime"] = current_regime
                    
                    if vwap_cache.get(f"REV_{t}_loc_fired"):
                        continue

                    rev_daily_budget = float(cfg.get_seed(t) or 0.0) * 0.15
                    
                    rev_plan = strategy_rev.get_dynamic_plan(
                        ticker=t, curr_p=curr_p, prev_c=prev_c, 
                        current_weight=current_weight, vwap_status=vwap_status, 
                        min_idx=min_idx, alloc_cash=rev_daily_budget, q_data=q_data
                    )
                    
                    # 💡 [핵심 수술 5] LOC 전환 기준점을 장 마감 "15분 전 이상 남았을 때"로 동적 처리
                    if rev_plan.get('trigger_loc') and minutes_to_close >= 15:
                        vwap_cache[f"REV_{t}_loc_fired"] = True
                        msg = f"🛡️ <b>[{t}] 60% 거래량 지배력 감지 (추세장 전환)</b>\n"
                        msg += f"▫️ 기관급 자금 쏠림으로 인해 위험한 1분 단위 타임 슬라이싱(VWAP)을 전면 중단합니다.\n"
                        msg += f"▫️ <b>잔여 할당량 전량을 양방향 LOC 방어선으로 전환 배치 완료!</b>\n"
                        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML', disable_notification=True)
                        
                        for o in rev_plan.get('orders', []):
                            if o['qty'] > 0:
                                broker.send_order(t, o['side'], o['qty'], o['price'], "LOC")
                                await asyncio.sleep(0.2)
                        continue
                        
                    for o in rev_plan.get('orders', []):
                        slice_qty = o['qty']
                        if slice_qty <= 0: continue
                        
                        target_price = o['price']
                        side = o['side']
                        
                        ask_price = float(await asyncio.to_thread(broker.get_ask_price, t) or 0.0)
                        bid_price = float(await asyncio.to_thread(broker.get_bid_price, t) or 0.0)
                        exec_price = ask_price if side == "BUY" else bid_price
                        if exec_price <= 0: exec_price = curr_p
                        
                        if side == "BUY" and exec_price > target_price: continue
                        if side == "SELL" and exec_price < target_price: continue
                        
                        res = broker.send_order(t, side, slice_qty, exec_price, "LIMIT")
                        odno = res.get('odno', '')
                        
                        if res.get('rt_cd') == '0' and odno:
                            ccld_qty = 0
                            for _ in range(4):
                                await asyncio.sleep(2.0)
                                unfilled_check = await asyncio.to_thread(broker.get_unfilled_orders_detail, t)
                                my_order = next((ox for ox in unfilled_check if ox.get('odno') == odno), None)
                                if my_order:
                                    ccld_qty = int(float(my_order.get('tot_ccld_qty') or 0))
                                    break
                                    
                                execs = await asyncio.to_thread(broker.get_execution_history, t, today_str, today_str)
                                my_execs = [ex for ex in execs if ex.get('odno') == odno]
                                if my_execs:
                                    ccld_qty = sum(int(float(ex.get('ft_ccld_qty') or 0)) for ex in my_execs)
                                    if ccld_qty >= slice_qty: break
                                    
                            if ccld_qty < slice_qty:
                                await asyncio.to_thread(broker.cancel_order, t, odno)
                                await asyncio.sleep(1.0)
                                
                            if ccld_qty > 0:
                                strategy_rev.record_execution(t, side, ccld_qty, exec_price)
                                if side == "BUY":
                                    queue_ledger.add_lot(t, ccld_qty, exec_price, "VWAP_BUY")
                                elif side == "SELL":
                                    queue_ledger.pop_lots(t, ccld_qty)
                                    
                            await asyncio.sleep(0.2)

    try:
        await asyncio.wait_for(_do_vwap(), timeout=45.0)
    except Exception as e:
        logging.error(f"🚨 VWAP 스케줄러 에러: {e}")

# ==========================================================
# 4. 🌅 정규장 오픈 (17:05) 전송 (V14 통합 & V-REV 예방 방어선)
# ==========================================================
async def scheduled_regular_trade(context):
    kst = pytz.timezone('Asia/Seoul')
    now = datetime.datetime.now(kst)
    target_hour, _ = get_target_hour()
    chat_id = context.job.chat_id
    
    now_minutes = now.hour * 60 + now.minute
    target_minutes = target_hour * 60 + 5
    
    if abs(now_minutes - target_minutes) > 2 and abs(now_minutes - target_minutes) < (24*60 - 2):
        return
        
    if not is_market_open():
        return
    
    app_data = context.job.data
    cfg, broker, strategy, tx_lock = app_data['cfg'], app_data['broker'], app_data['strategy'], app_data['tx_lock']
    strategy_rev = app_data.get('strategy_rev')
    queue_ledger = app_data.get('queue_ledger')
    
    jitter_seconds = random.randint(0, 180)

    await context.bot.send_message(
        chat_id=chat_id, 
        text=f"🌃 <b>[{target_hour}:05] 통합 주문 장전!</b>\n"
             f"🛡️ 서버 접속 부하 방지를 위해 <b>{jitter_seconds}초</b> 대기 후 안전하게 주문 전송을 시도합니다.", 
        parse_mode='HTML'
    )

    await asyncio.sleep(jitter_seconds)

    MAX_RETRIES = 15
    RETRY_DELAY = 60

    async def _do_regular_trade():
        async with tx_lock:
            cash, holdings = broker.get_account_balance()
            if holdings is None:
                return False, "❌ 계좌 정보를 불러오지 못했습니다."

            sorted_tickers, allocated_cash = get_budget_allocation(cash, cfg.get_active_tickers(), cfg)
            
            plans = {}
            msgs = {t: "" for t in sorted_tickers}
            all_success = {t: True for t in sorted_tickers}
            v_rev_tickers = []

            for t in sorted_tickers:
                if cfg.check_lock(t, "REG"): continue
                
                h = holdings.get(t) or {}
                curr_p = float(await asyncio.to_thread(broker.get_current_price, t) or 0.0)
                prev_c = float(await asyncio.to_thread(broker.get_previous_close, t) or 0.0)
                safe_avg = float(h.get('avg') or 0.0)
                safe_qty = int(float(h.get('qty') or 0))
                
                if cfg.get_version(t) == "V_REV":
                    q_data = queue_ledger.get_queue(t)
                    v_rev_q_qty = sum(item.get("qty", 0) for item in q_data)
                    rev_budget = float(cfg.get_seed(t) or 0.0) * 0.15
                    
                    half_portion_cash = rev_budget * 0.5
                    one_portion_qty = math.floor(rev_budget / curr_p) if curr_p > 0 else 0
                    
                    loc_orders = []
                    
                    if q_data:
                        recent_lots = list(reversed(q_data))[:3]
                        for idx, lot in enumerate(recent_lots):
                            if idx == 0:
                                target_sell_price = round(lot.get('price', prev_c) * 1.006, 2)
                            else:
                                target_sell_price = round(safe_avg * 1.005, 2)
                                
                            sell_qty = min(lot['qty'], one_portion_qty) if one_portion_qty > 0 else lot['qty']
                            if sell_qty > 0:
                                loc_orders.append({'side': 'SELL', 'qty': sell_qty, 'price': target_sell_price, 'type': 'LOC', 'desc': f'예방적 매도(Pop{idx+1})'})
                    
                    if prev_c > 0:
                        b1_price = round(prev_c * 0.999 if v_rev_q_qty == 0 else prev_c * 0.995, 2)
                        b2_price = round(prev_c / 0.935 if v_rev_q_qty == 0 else prev_c * 0.9725, 2)
                        
                        b1_qty = math.floor(half_portion_cash / b1_price) if b1_price > 0 else 0
                        b2_qty = math.floor(half_portion_cash / b2_price) if b2_price > 0 else 0
                        
                        if b1_qty > 0:
                            loc_orders.append({'side': 'BUY', 'qty': b1_qty, 'price': b1_price, 'type': 'LOC', 'desc': '예방적 매수(Buy1)'})
                        if b2_qty > 0:
                            loc_orders.append({'side': 'BUY', 'qty': b2_qty, 'price': b2_price, 'type': 'LOC', 'desc': '예방적 매수(Buy2)'})

                    plans[t] = {'core_orders': loc_orders, 'bonus_orders': [], 'is_reverse': False}
                    msgs[t] += f"🛡️ <b>[{t}] V-REV 예방적 양방향 LOC (Fail-Safe) 덫 장전</b>\n"
                    v_rev_tickers.append(t)
                    continue
                
                ma_5day = float(await asyncio.to_thread(broker.get_5day_ma, t) or 0.0)
                plan = strategy.get_plan(t, curr_p, safe_avg, safe_qty, prev_c, ma_5day=ma_5day, market_type="REG", available_cash=allocated_cash.get(t, 0.0))
                plans[t] = plan
                
                if plan.get('core_orders', []) or plan.get('orders', []):
                    is_rev = plan.get('is_reverse', False)
                    ver_txt = "정규장 주문"
                    msgs[t] += f"🔄 <b>[{t}] 리버스 주문 실행</b>\n" if is_rev else f"💎 <b>[{t}] {ver_txt} 실행</b>\n"

            for t in v_rev_tickers:
                msg = f"🎺 <b>[{t}] V-REV 예방적 방어망 장전 완료</b>\n"
                msg += f"▫️ 프리장이 개장했습니다! 시스템 다운 등 최악의 블랙스완을 대비하여 <b>양방향 종가(LOC) 덫</b>을 KIS 서버에 선제 전송했습니다.\n"
                msg += f"▫️ 서버가 무사하다면 장 후반(04:30 KST)에 스스로 깨어나 이 덫을 거두고 추세(60% 허들)를 스캔하여 새로운 최적 전술로 교체합니다! 편안한 밤 보내십시오! 🌙💤\n"
                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')

            for t in sorted_tickers:
                if t not in plans: continue
                target_orders = plans[t].get('core_orders', plans[t].get('orders', []))
                if not target_orders: continue
                
                for o in target_orders:
                    res = broker.send_order(t, o['side'], o['qty'], o['price'], o['type'])
                    is_success = res.get('rt_cd') == '0'
                    if not is_success: all_success[t] = False
                    err_msg = res.get('msg1')
                    msgs[t] += f"└ 1차 필수: {o['desc']} {o['qty']}주: {'✅' if is_success else f'❌({err_msg})'}\n"
                    await asyncio.sleep(0.2) 

            for t in sorted_tickers:
                if t not in plans: continue
                target_bonus = plans[t].get('bonus_orders', [])
                if not target_bonus: continue
                
                for o in target_bonus:
                    res = broker.send_order(t, o['side'], o['qty'], o['price'], o['type'])
                    is_success = res.get('rt_cd') == '0'
                    msgs[t] += f"└ 2차 보너스: {o['desc']} {o['qty']}주: {'✅' if is_success else '❌(잔금패스)'}\n"
                    await asyncio.sleep(0.2) 

            for t in sorted_tickers:
                if t not in plans: continue
                target_orders = plans[t].get('core_orders', plans[t].get('orders', []))
                target_bonus = plans[t].get('bonus_orders', [])
                
                if not target_orders and not target_bonus: continue
                
                if all_success[t] and len(target_orders) > 0:
                    cfg.set_lock(t, "REG")
                    msgs[t] += "\n🔒 <b>필수 주문 정상 전송 완료 (잠금 설정됨)</b>"
                elif not all_success[t] and len(target_orders) > 0:
                    msgs[t] += "\n⚠️ <b>일부 필수 주문 실패 (매매 잠금 보류)</b>"
                elif len(target_bonus) > 0:
                    cfg.set_lock(t, "REG")
                    msgs[t] += "\n🔒 <b>보너스 주문만 전송 완료 (잠금 설정됨)</b>"
                    
                if t not in v_rev_tickers: 
                    await context.bot.send_message(chat_id=chat_id, text=msgs[t], parse_mode='HTML')

            return True, "SUCCESS"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            success, fail_reason = await asyncio.wait_for(_do_regular_trade(), timeout=300.0)
            if success:
                if attempt > 1:
                    await context.bot.send_message(chat_id=chat_id, text=f"✅ <b>[통신 복구] {attempt}번째 재시도 끝에 전송을 완수했습니다!</b>", parse_mode='HTML')
                return 
        except Exception as e:
            logging.error(f"정규장 전송 에러 ({attempt}/{MAX_RETRIES}): {e}")

        if attempt < MAX_RETRIES:
            if attempt == 1 or attempt % 5 == 0:
                await context.bot.send_message(chat_id=chat_id, text=f"⚠️ <b>[API 통신 지연 감지]</b>\n한투 서버 불안정. 1분 뒤 재시도합니다! 🛡️", parse_mode='HTML')
            await asyncio.sleep(RETRY_DELAY)

    await context.bot.send_message(chat_id=chat_id, text="🚨 <b>[긴급 에러] 통신 복구 최종 실패. 수동 점검 요망!</b>", parse_mode='HTML')

# ==========================================================
# 5. 🌙 애프터마켓 로터리 덫 (16:05 EST / 05:05 KST)
# ==========================================================
async def scheduled_after_market_lottery(context):
    app_data = context.job.data
    cfg, broker, tx_lock = app_data['cfg'], app_data['broker'], app_data['tx_lock']
    chat_id = context.job.chat_id

    async def _do_lottery():
        async with tx_lock:
            cash, holdings = broker.get_account_balance()
            if holdings is None: return

            for t in cfg.get_active_tickers():
                if cfg.get_version(t) != "V_REV":
                    continue

                h = holdings.get(t) or {}
                qty = int(float(h.get('qty') or 0))
                avg_price = float(h.get('avg') or 0.0)

                if qty > 0 and avg_price > 0:
                    target_price = math.ceil(avg_price * 1.030 * 100) / 100.0

                    await asyncio.to_thread(broker.cancel_all_orders_safe, t, "SELL")
                    await asyncio.sleep(0.5)

                    res = broker.send_order(t, "SELL", qty, target_price, "AFTER_LIMIT")
                    
                    if res.get('rt_cd') == '0':
                        msg = f"🌙 <b>[{t}] 애프터마켓 3% 로터리 덫(Lottery Trap) 장전 완료</b>\n"
                        msg += f"▫️ 대상 물량: <b>{qty}주</b> 전량\n"
                        msg += f"▫️ 타겟 가격: <b>${target_price:.2f}</b> (총 평단가 +3%)\n"
                        msg += f"▫️ 정규장 마감 후 유휴 주식을 활용하여 시간 외 폭등을 포획합니다. 미체결 시 내일 아침 자동 소멸됩니다! 🎣"
                        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML', disable_notification=True)
                    await asyncio.sleep(0.2)

    try:
        await asyncio.wait_for(_do_lottery(), timeout=60.0)
    except Exception as e:
        logging.error(f"🚨 애프터마켓 로터리 덫 에러: {e}")

