# ==========================================================
# [telegram_sync_engine.py] - 🌟 100% 통합 완성본 🌟 (Part 1)
# MODIFIED: [V28.10 장부 환각 엣지 케이스 수술] 실잔고와 큐 장부 수량이 일치할 경우
# 비파괴 보정(CALIB) 호출을 원천 차단하는 멱등성 락온(Idempotency Lock-on) 방어막 이식.
# 이로써 21주가 42주로 단순 중복 덧셈되던 치명적 환각 버그 영구 소각 완료.
# MODIFIED: [V28.21 동기화 엇박자 그랜드 수술] 졸업 판별(0주 스캔) 전, 
# 당일 KIS 매도 체결 영수증을 장부에 우선 기록하도록 파이프라인 순서(Order)를 
# 전면 뒤집어 락온함. 이로써 매도액 누락 및 수익률 -100% 환각 버그 원천 차단.
# MODIFIED: [V28.23 타임존 락온 그랜드 수술] KST 기준 날짜 연산을 전면 폐기하고,
# EST(미국 동부) 기준으로 100% 형변환하여 타임 패러독스로 인한 체결 내역 증발 및 
# 스냅샷 매핑 실패 버그를 영구 소각 완료. (EC-1, EC-3 방어)
# ==========================================================
# NEW: [리팩토링 1단계] 핵심 비즈니스 코어(장부 동기화, 졸업 판별, 큐 관리) 독립 클래스로 캡슐화
import logging
import datetime
import pytz
import time
import os
import asyncio
import json
import yfinance as yf
import pandas_market_calendars as mcal
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

class TelegramSyncEngine:
    def __init__(self, config, broker, strategy, queue_ledger, view, tx_lock, sync_locks):
        self.cfg = config
        self.broker = broker
        self.strategy = strategy
        self.queue_ledger = queue_ledger
        self.view = view
        self.tx_lock = tx_lock
        self.sync_locks = sync_locks

    def _sync_escrow_cash(self, ticker):
        is_rev = self.cfg.get_reverse_state(ticker).get("is_active", False)
        if not is_rev:
            self.cfg.clear_escrow_cash(ticker)
            return

        ledger = self.cfg.get_ledger()
        
        target_recs = []
        for r in reversed(ledger):
            if r.get('ticker') == ticker:
                if r.get('is_reverse', False):
                    target_recs.append(r)
                else:
                    break
        
        escrow = 0.0
        for r in target_recs:
            amt = r['qty'] * r['price']
            if r['side'] == 'SELL':
                escrow += amt
            elif r['side'] == 'BUY':
                escrow -= amt
                
        self.cfg.set_escrow_cash(ticker, max(0.0, escrow))

    async def process_auto_sync(self, ticker, chat_id, context, silent_ledger=False):
        if ticker not in self.sync_locks:
            self.sync_locks[ticker] = asyncio.Lock()
            
        if self.sync_locks[ticker].locked(): 
            return "LOCKED"
            
        async with self.sync_locks[ticker]:
            async with self.tx_lock:
                
                last_split_date = self.cfg.get_last_split_date(ticker)
                split_ratio, split_date = await asyncio.to_thread(self.broker.get_recent_stock_split, ticker, last_split_date)
                
                if split_ratio > 0.0 and split_date != "":
                    self.cfg.apply_stock_split(ticker, split_ratio)
                    self.cfg.set_last_split_date(ticker, split_date)
                    split_type = "액면분할" if split_ratio > 1.0 else "액면병합(역분할)"
                    await context.bot.send_message(chat_id, f"✂️ <b>[{ticker}] 야후 파이낸스 {split_type} 자동 감지!</b>\n▫️ 감지된 비율: <b>{split_ratio}배</b> (발생일: {split_date})\n▫️ 봇이 기존 장부의 수량과 평단가를 100% 무인 자동 소급 조정 완료했습니다.", parse_mode='HTML')
                
                # 🚨 [AI 에이전트(Copilot/Claude) 절대 주의 - 타임존 락온 방어막 (EC-1)]
                # 서버 시스템 시간(KST)을 기준으로 NYSE 체결 내역을 조회하는 Fallback 로직을 전면 소각함.
                # KST 자정 ~ 14:00 구간에서 캘린더가 텅 비었을 때(schedule.empty), KST 기준으로 날짜를 산출하면
                # '아직 열리지 않은 미래의 미국 날짜'가 산출되어 체결 내역이 0건으로 증발(CALIB 누락)하는 버그가 발생함.
                # 이를 방지하기 위해 Fallback 날짜마저도 무조건 EST(now_est) 기준으로 강제 고정함.
                kst = pytz.timezone('Asia/Seoul')
                now_kst = datetime.datetime.now(kst)
                
                est = pytz.timezone('US/Eastern')
                now_est = datetime.datetime.now(est)
                nyse = mcal.get_calendar('NYSE')
                schedule = nyse.schedule(start_date=(now_est - datetime.timedelta(days=10)).date(), end_date=now_est.date())
                
                if not schedule.empty:
                    last_trade_date = schedule.index[-1]
                    target_kis_str = last_trade_date.strftime('%Y%m%d')
                    target_ledger_str = last_trade_date.strftime('%Y-%m-%d')
                else:
                    # MODIFIED: [EC-1 방어] schedule.empty 일 때 KST가 아닌 EST 날짜 사용
                    target_kis_str = now_est.strftime('%Y%m%d')
                    target_ledger_str = now_est.strftime('%Y-%m-%d')

                _, holdings = self.broker.get_account_balance()
                if holdings is None:
                    await context.bot.send_message(chat_id, f"❌ <b>[{ticker}] API 오류</b>\n잔고를 불러오지 못했습니다.", parse_mode='HTML')
                    return "ERROR"

                actual_qty = int(float(holdings.get(ticker, {'qty': 0}).get('qty') or 0))
                actual_avg = float(holdings.get(ticker, {'avg': 0}).get('avg') or 0.0)

                # ==========================================================
                # MODIFIED: [V28.21 동기화 엇박자 그랜드 수술] 파이프라인 순서 역전
                # 졸업 판별(actual_qty == 0) 이전에 KIS 당일 체결 내역을 장부에 우선 동기화하도록 로직 블록을 위로 끌어올림.
                # ==========================================================
                target_execs = await asyncio.to_thread(self.broker.get_execution_history, ticker, target_kis_str, target_kis_str)
                if target_execs:
                    calibrated_count = self.cfg.calibrate_ledger_prices(ticker, target_ledger_str, target_execs)
                    if calibrated_count > 0:
                        logging.info(f"🔧 [{ticker}] LOC/MOC 주문 {calibrated_count}건에 대해 실제 체결 단가 소급 업데이트를 완료했습니다.")

                recs = [r for r in self.cfg.get_ledger() if r['ticker'] == ticker]
                ledger_qty, avg_price, _, _ = self.cfg.calculate_holdings(ticker, recs)
                
                diff = actual_qty - ledger_qty
                price_diff = abs(actual_avg - avg_price)

                # V-REV 모드가 아닐 때(V14) 0주가 아니더라도 오차가 있으면 비파괴 보정을 먼저 쳐둠
                if self.cfg.get_version(ticker) != "V_REV":
                    if diff == 0 and price_diff < 0.01:
                        pass 
                    elif diff == 0 and price_diff >= 0.01:
                        self.cfg.calibrate_avg_price(ticker, actual_avg)
                        await context.bot.send_message(chat_id, f"🔧 <b>[{ticker}] 장부 평단가 미세 오차({price_diff:.4f}) 교정 완료!</b>", parse_mode='HTML')
                    elif diff != 0:
                        temp_recs = [r for r in recs if r['date'] != target_ledger_str or 'INIT' in str(r.get('exec_id', ''))]
                        temp_qty, temp_avg, _, _ = self.cfg.calculate_holdings(ticker, temp_recs)
                        
                        temp_sim_qty = temp_qty
                        temp_sim_avg = temp_avg
                        new_target_records = []
                        
                        if target_execs:
                            target_execs.sort(key=lambda x: x.get('ord_tmd', '000000')) 
                            for ex in target_execs:
                                side_cd = ex.get('sll_buy_dvsn_cd')
                                exec_qty = int(float(ex.get('ft_ccld_qty', '0')))
                                exec_price = float(ex.get('ft_ccld_unpr3', '0'))
                                
                                if side_cd == "02": 
                                    new_avg = ((temp_sim_qty * temp_sim_avg) + (exec_qty * exec_price)) / (temp_sim_qty + exec_qty) if (temp_sim_qty + exec_qty) > 0 else exec_price
                                    temp_sim_qty += exec_qty
                                    temp_sim_avg = new_avg
                                else:
                                    temp_sim_qty -= exec_qty
                                    
                                new_target_records.append({
                                    'date': target_ledger_str, 'side': "BUY" if side_cd == "02" else "SELL",
                                    'qty': exec_qty, 'price': exec_price, 'avg_price': temp_sim_avg
                                })
                                
                        gap_qty = actual_qty - temp_sim_qty
                        if gap_qty != 0:
                            calib_side = "BUY" if gap_qty > 0 else "SELL"
                            new_target_records.append({
                                'date': target_ledger_str, 
                                'side': calib_side,
                                'qty': abs(gap_qty), 
                                'price': actual_avg, 
                                'avg_price': actual_avg,
                                'exec_id': f"CALIB_{int(time.time())}",
                                'desc': "비파괴 보정"
                            })
                            
                        if new_target_records:
                            for r in new_target_records:
                                r['avg_price'] = actual_avg
                        elif temp_recs: 
                            temp_recs[-1]['avg_price'] = actual_avg
                            
                        self.cfg.overwrite_incremental_ledger(ticker, temp_recs, new_target_records)
                        
                        if gap_qty != 0:
                            await context.bot.send_message(chat_id, f"🔧 <b>[{ticker}] 비파괴 장부 보정 완료!</b>\n▫️ 오차 수량({gap_qty}주)을 기존 역사 보존 상태로 안전하게 교정했습니다.", parse_mode='HTML')

                # ==========================================================
                # V-REV 큐 관리 및 0주 졸업 판별 로직 시작
                # ==========================================================
                if self.cfg.get_version(ticker) == "V_REV":
                    if not getattr(self, 'queue_ledger', None):
                        from queue_ledger import QueueLedger
                        self.queue_ledger = QueueLedger()
                    
                    q_data_before = self.queue_ledger.get_queue(ticker)
                    vrev_ledger_qty = sum(int(float(item.get("qty") or 0)) for item in q_data_before)
                    
                    if actual_qty == 0 and vrev_ledger_qty > 0:
                        if now_kst.hour < 10:
                            await context.bot.send_message(chat_id, "⏳ <b>증권사 확정 정산(10:00 KST) 대기 중입니다.</b> 가결제 오차 방지를 위해 졸업 카드 발급 및 장부 초기화가 보류됩니다.", parse_mode='HTML')
                            self._sync_escrow_cash(ticker)
                            return "SUCCESS"

                        added_seed = 0.0
                        _vrev_snap_ok = False
                        snapshot = None
                        try:
                            total_invested = sum(float(item.get("qty", 0)) * float(item.get("price", 0)) for item in q_data_before)
                            q_avg_price = total_invested / vrev_ledger_qty if vrev_ledger_qty > 0 else 0.0
                            
                            curr_p = await asyncio.to_thread(self.broker.get_current_price, ticker)
                            clear_price = curr_p if curr_p and curr_p > 0 else q_avg_price * 1.006 
                            
                            snapshot = self.strategy.capture_vrev_snapshot(ticker, clear_price, q_avg_price, vrev_ledger_qty)
                            
                            if snapshot:
                                realized_pnl = snapshot['realized_pnl']
                                yield_pct = snapshot['realized_pnl_pct']
                                
                                compound_rate = float(self.cfg.get_compound_rate(ticker)) / 100.0
                                if realized_pnl > 0 and compound_rate > 0:
                                    added_seed = realized_pnl * compound_rate
                                    current_seed = self.cfg.get_seed(ticker)
                                    self.cfg.set_seed(ticker, current_seed + added_seed)
                                
                                hist_data = self.cfg._load_json(self.cfg.FILES["HISTORY"], [])
                                new_hist = {
                                    "id": int(time.time()),
                                    "ticker": ticker,
                                    "start_date": q_data_before[-1]['date'][:10] if q_data_before else snapshot['captured_at'].strftime('%Y-%m-%d'),
                                    "end_date": snapshot['captured_at'].strftime('%Y-%m-%d'),
                                    "invested": total_invested,
                                    "revenue": total_invested + realized_pnl,
                                    "profit": realized_pnl,
                                    "yield": yield_pct,
                                    "trades": q_data_before 
                                }
                                hist_data.append(new_hist)
                                self.cfg._save_json(self.cfg.FILES["HISTORY"], hist_data)
                                _vrev_snap_ok = True
                                
                        except Exception as e:
                            logging.error(f"스냅샷 캡처 및 복리 정산 중 오류: {e}")
                            snapshot = None
                            
                        if _vrev_snap_ok:
                            self.queue_ledger.sync_with_broker(ticker, 0)
                            
                            msg = f"🎉 <b>[{ticker} V-REV 잭팟 스윕(전량 익절) 감지!]</b>\n▫️ 잔고가 0주가 되어 LIFO 큐 지층을 100% 소각(초기화)했습니다."
                            if added_seed > 0:
                                msg += f"\n💸 <b>자동 복리 +${added_seed:,.0f}</b> 이 다음 운용 시드에 완벽하게 추가되었습니다!"
                            await context.bot.send_message(chat_id, msg, parse_mode='HTML')
                            
                            if snapshot:
                                try:
                                    img_path = self.view.create_profit_image(
                                        ticker=ticker, 
                                        profit=snapshot['realized_pnl'], 
                                        yield_pct=snapshot['realized_pnl_pct'],
                                        invested=snapshot['avg_price'] * snapshot['cleared_qty'], 
                                        revenue=snapshot['clear_price'] * snapshot['cleared_qty'], 
                                        end_date=snapshot['captured_at'].strftime('%Y-%m-%d')
                                    )
                                    if img_path and os.path.exists(img_path):
                                        with open(img_path, 'rb') as f_out:
                                            if img_path.lower().endswith('.gif'):
                                                await context.bot.send_animation(chat_id=chat_id, animation=f_out)
                                            else:
                                                await context.bot.send_photo(chat_id=chat_id, photo=f_out)
                                except Exception as e:
                                    logging.error(f"📸 V-REV 스냅샷 이미지 렌더링/발송 실패: {e}")
                                    
                        self._sync_escrow_cash(ticker)
                        return "SUCCESS"
                        
                    if actual_qty == vrev_ledger_qty:
                        pass
                    else:
                        calibrated = self.queue_ledger.sync_with_broker(ticker, actual_qty, actual_avg)
                        if calibrated:
                            await context.bot.send_message(chat_id, f"🔧 <b>[{ticker}] V-REV 큐(Queue) 비파괴 보정(CALIB) 완료!</b>\n▫️ KIS 실제 잔고(<b>{actual_qty}주</b>)에 맞춰 LIFO 지층을 정밀 차감/추가했습니다.", parse_mode='HTML')
                    
                    self._sync_escrow_cash(ticker)
                    return "SUCCESS"

                # ==========================================================
                # V14 0주 졸업 판별 로직 (동기화 파이프라인 수술 후 위치 이동)
                # ==========================================================
                if actual_qty == 0:
                    if ledger_qty > 0:
                        if now_kst.hour < 10:
                            await context.bot.send_message(chat_id, "⏳ <b>증권사 확정 정산(10:00 KST) 대기 중입니다.</b> 가결제 오차 방지를 위해 졸업 카드 발급 및 장부 초기화가 보류됩니다.", parse_mode='HTML')
                        else:
                            # 🚨 [AI 에이전트(Copilot/Claude) 절대 주의 - 타임존 락온 방어막 (EC-2)]
                            # V14 모드 졸업 아카이빙 시 넘겨주는 날짜를 KST에서 EST로 강제 형변환 완료.
                            # KST 자정 경계선에서 KST 날짜("2026-04-19")를 인자로 넘기면, 
                            # config.py 내부에서 "2026-04-18"로 생성된 EST 기준 스냅샷 파일을 찾지 못해
                            # 졸업 PnL 데이터가 증발하는 치명적 맹점을 원천 차단함.
                            today_est_str = now_est.strftime('%Y-%m-%d')
                            prev_c = await asyncio.to_thread(self.broker.get_previous_close, ticker)
                            
                            try:
                                # MODIFIED: [EC-2 방어] today_str(KST) 대신 today_est_str(EST) 인자 전달
                                new_hist, added_seed = self.cfg.archive_graduation(ticker, today_est_str, prev_c)
                                
                                if new_hist:
                                    msg = f"🎉 <b>[{ticker} 졸업 확인!]</b>\n장부를 명예의 전당에 저장하고 새 사이클을 준비합니다."
                                    if added_seed > 0:
                                        msg += f"\n💸 <b>자동 복리 +${added_seed:,.0f}</b> 이 다음 운용 시드에 완벽하게 추가되었습니다!"
                                    await context.bot.send_message(chat_id, msg, parse_mode='HTML')
                                    try:
                                        img_path = self.view.create_profit_image(
                                            ticker=ticker, profit=new_hist['profit'], yield_pct=new_hist['yield'],
                                            invested=new_hist['invested'], revenue=new_hist['revenue'], end_date=new_hist['end_date']
                                        )
                                        if img_path and os.path.exists(img_path):
                                            with open(img_path, 'rb') as f_out:
                                                if img_path.lower().endswith('.gif'):
                                                    await context.bot.send_animation(chat_id=chat_id, animation=f_out)
                                                else:
                                                    await context.bot.send_photo(chat_id=chat_id, photo=f_out)
                                    except Exception as e:
                                        logging.error(f"📸 졸업 이미지 발송 실패: {e}")
                                else:
                                    all_recs = [r for r in self.cfg.get_ledger() if r['ticker'] != ticker]
                                    self.cfg._save_json(self.cfg.FILES["LEDGER"], all_recs)
                                    await context.bot.send_message(chat_id, f"⚠️ <b>[{ticker} 강제 정산 완료]</b>\n잔고가 0주이나 마이너스 수익 상태이므로 명예의 전당 박제 없이 장부를 비우고 새출발 타점을 장전합니다.", parse_mode='HTML')
                            except Exception as e:
                                logging.error(f"강제 졸업 처리 중 에러: {e}")

                    self._sync_escrow_cash(ticker) 
                    return "SUCCESS"

                self._sync_escrow_cash(ticker)
                return "SUCCESS"

    async def _display_ledger(self, ticker, chat_id, context, query=None, message_obj=None, pre_fetched_holdings=None):
        recs = [r for r in self.cfg.get_ledger() if r['ticker'] == ticker]
        
        if not recs:
            msg = f"📭 <b>[{ticker}]</b> 현재 진행 중인 사이클이 없습니다 (보유량 0주)."
        else:
            from collections import OrderedDict
            agg_dict = OrderedDict()
            total_buy = 0.0
            total_sell = 0.0
            
            for rec in recs:
                parts = rec['date'].split('-')
                if len(parts) == 3:
                    date_short = f"{parts[1]}.{parts[2]}"
                else:
                    date_short = rec['date']
                    
                side_str = "🔴매수" if rec['side'] == 'BUY' else "🔵매도"
                key = (date_short, side_str)
                
                if key not in agg_dict:
                    agg_dict[key] = {'qty': 0, 'amt': 0.0}
                    
                agg_dict[key]['qty'] += rec['qty']
                agg_dict[key]['amt'] += (rec['qty'] * rec['price'])
                
                if rec['side'] == 'BUY':
                    total_buy += (rec['qty'] * rec['price'])
                elif rec['side'] == 'SELL':
                    total_sell += (rec['qty'] * rec['price'])
            
            report = f"📜 <b>[ {ticker} 일자별 매매 (통합 변동분) (총 {len(agg_dict)}일) ]</b>\n\n<code>No. 일자   구분  평균단가  수량\n"
            report += "-"*30 + "\n"
            
            idx = 1
            for (date, side), data in agg_dict.items():
                tot_qty = data['qty']
                avg_prc = data['amt'] / tot_qty if tot_qty > 0 else 0.0
                report += f"{idx:<3} {date} {side} ${avg_prc:<6.2f} {tot_qty}주\n"
                idx += 1
                
            report += "-"*30 + "</code>\n"
            
            actual_qty = int(float(pre_fetched_holdings.get(ticker, {'qty': 0})['qty'] or 0)) if pre_fetched_holdings else 0
            actual_avg = float(pre_fetched_holdings.get(ticker, {'avg': 0})['avg'] or 0.0) if pre_fetched_holdings else 0.0
            
            split = self.cfg.get_split_count(ticker)
            t_val, _ = self.cfg.get_absolute_t_val(ticker, actual_qty, actual_avg)
            
            report += "📊 <b>[ 현재 진행 상황 요약 ]</b>\n"
            report += f"▪️ 현재 T값 : {t_val:.4f} T ({int(split)}분할)\n"
            report += f"▪️ 보유 수량 : {actual_qty} 주 (평단 ${actual_avg:,.2f})\n"
            report += f"▪️ 총 매수액 : ${total_buy:,.2f}\n"
            report += f"▪️ 총 매도액 : ${total_sell:,.2f}"
            
            msg = report

        tickers = self.cfg.get_active_tickers()
        keyboard = []
        
        if self.cfg.get_version(ticker) == "V_REV":
            keyboard.append([InlineKeyboardButton(f"🗄️ {ticker} V-REV 큐(Queue) 정밀 관리", callback_data=f"QUEUE:VIEW:{ticker}")])
            
        row = [InlineKeyboardButton(f"🔄 {t} 장부 업데이트", callback_data=f"REC:SYNC:{t}") for t in tickers]
        keyboard.append(row)
        markup = InlineKeyboardMarkup(keyboard)

        if query:
            await query.edit_message_text(msg, reply_markup=markup, parse_mode='HTML')
        elif message_obj:
            await message_obj.edit_text(msg, reply_markup=markup, parse_mode='HTML')
        else:
            await context.bot.send_message(chat_id, msg, reply_markup=markup, parse_mode='HTML')
