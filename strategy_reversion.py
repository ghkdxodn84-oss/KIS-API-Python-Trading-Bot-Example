# ==========================================================
# [strategy_reversion.py]
# ⚠️ V-REV 하이브리드 엔진 전용 수학적 타격 모듈
# 💡 5년 백테스트 기반 VWAP 유동성 정밀 가중치(U_CURVE_WEIGHTS) 적용 완료
# 💡 [V24.16 팩트 동기화] 0주 새출발 디커플링 타점 (Buy1: 0.999, Buy2: /0.935) 원본 유지
# 💡 [V24.16 팩트 동기화] 하락장 방어 매수 Buy2 타점 (0.9725) 교정
# 💡 [V24.16 팩트 동기화] 1층 전량 익절 타점 고유 매수가 기반(layer_price * 1.006) 원복
# 🚨 [V25.13 디커플링 스왑 패치] UI와 동일하게 Buy1과 Buy2의 타점을 고가->저가 순으로 스왑 연동
# 🚨 [V25.14 팩트 동기화] 1층 물귀신 덤핑 차단 및 지층별 평단가 완벽 분리 개별 탈출(Decoupling) 이식
# 🚨 [V25.15 잔여물량 격리] SELL_L1 / SELL_UPPER / SELL_JACKPOT 독립 큐(Residual) 분리 및 줍줍 무손실 복원 완료
# 🚨 [V25.17 잔재 소각] 수동 통제망(Telegram) 전환에 따른 자동 긴급 수혈(get_emergency_liquidation_qty) 레거시 함수 영구 삭제
# 🚨 [V25.20 엣지 케이스 패치] 0주 새출발 시 줍줍(Sweep) 타점 생성 원천 차단 (단일 라우터 방어막 이식)
# 🚀 [V26.03 영속성 캐시 이식] 서버 재시작 시 잔차 증발(기억상실)을 방어하는 L1/L2 듀얼 캐싱 엔진 탑재
# 🚀 [V27.01 지시서 스냅샷] 매일 17:05 확정 지시서를 박제하여 장중 잔고 변이에 따른 타점 왜곡 원천 차단
# 🚨 [V27.03 핫픽스] 스냅샷 로드 시 내부 날짜 검사(Validation) 전면 폐기로 무한루프 영구 방어
# 🚨 [V27.05 그랜드 수술] API Reject 방어(소수점 덤핑 차단), ZeroDivision 방어 및 Safe Casting 완벽 이식
# 🚨 [V27.15 코파일럿 합작] FD 누수 방어, 스냅샷 덮어쓰기 락온, 0달러 로트 배제 및 TypeError 런타임 붕괴 방어막 이식 완료
# 🚨 [V27.16 엣지 케이스 수술] 자전거래(Wash Trade) 원천 차단 방어막(Dynamic Shield) 이식 완료
# ==========================================================
import math
import os
import json
import tempfile
from datetime import datetime

class ReversionStrategy:
    def __init__(self):
        self.residual = {
            "BUY1": {}, "BUY2": {}, 
            "SELL_L1": {}, "SELL_UPPER": {}, "SELL_JACKPOT": {}
        }
        self.executed = {"BUY_BUDGET": {}, "SELL_QTY": {}}
        self.state_loaded = {}
        
        self.U_CURVE_WEIGHTS = [
            0.0252, 0.0213, 0.0192, 0.0210, 0.0189, 0.0187, 0.0228, 0.0203, 0.0200, 0.0209,
            0.0254, 0.0217, 0.0225, 0.0211, 0.0228, 0.0281, 0.0262, 0.0240, 0.0236, 0.0256,
            0.0434, 0.0294, 0.0327, 0.0362, 0.0549, 0.0566, 0.0407, 0.0470, 0.0582, 0.1515
        ]

    def _get_state_file(self, ticker):
        today_str = datetime.now().strftime("%Y-%m-%d")
        return f"data/vwap_state_REV_{today_str}_{ticker}.json"

    def _get_snapshot_file(self, ticker):
        today_str = datetime.now().strftime("%Y-%m-%d")
        return f"data/daily_snapshot_REV_{today_str}_{ticker}.json"

    def _load_state_if_needed(self, ticker):
        today_str = datetime.now().strftime("%Y-%m-%d")
        if self.state_loaded.get(ticker) == today_str:
            return 
            
        state_file = self._get_state_file(ticker)
        if os.path.exists(state_file):
            try:
                with open(state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for k in self.residual.keys():
                        self.residual[k][ticker] = float(data.get("residual", {}).get(k, 0.0))
                    for k in self.executed.keys():
                        raw_val = data.get("executed", {}).get(k, 0)
                        self.executed[k][ticker] = int(raw_val) if k == "SELL_QTY" else float(raw_val)
                    self.state_loaded[ticker] = today_str
                    return
            except Exception:
                pass
                
        for k in self.residual.keys():
            self.residual[k][ticker] = 0.0
        self.executed["BUY_BUDGET"][ticker] = 0.0
        self.executed["SELL_QTY"][ticker] = 0
        self.state_loaded[ticker] = today_str

    def _save_state(self, ticker):
        today_str = datetime.now().strftime("%Y-%m-%d")
        state_file = self._get_state_file(ticker)
        data = {
            "date": today_str,
            "residual": {k: float(self.residual[k].get(ticker, 0.0)) for k in self.residual.keys()},
            "executed": {
                "BUY_BUDGET": float(self.executed.get("BUY_BUDGET", {}).get(ticker, 0.0)),
                "SELL_QTY": int(self.executed.get("SELL_QTY", {}).get(ticker, 0))
            }
        }
        temp_path = None
        try:
            dir_name = os.path.dirname(state_file)
            if dir_name and not os.path.exists(dir_name):
                os.makedirs(dir_name, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(dir=dir_name, text=True)
            # MODIFIED: [파일 디스크립터 누수 및 fsync 붕괴 방어] os.fsync(f.fileno()) 표준화 및 자원 해제 보장
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, state_file)
            temp_path = None
        except Exception:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def save_daily_snapshot(self, ticker, plan_data):
        snap_file = self._get_snapshot_file(ticker)
        # MODIFIED: [스냅샷 덮어쓰기 붕괴 방어] 당일 최초 1회 박제(Idempotency) 로직 적용하여 장중 변이 완벽 차단
        if os.path.exists(snap_file):
            return
            
        today_str = datetime.now().strftime("%Y-%m-%d")
        data = {
            "date": today_str,
            "plan": plan_data
        }
        temp_path = None
        try:
            dir_name = os.path.dirname(snap_file)
            if dir_name and not os.path.exists(dir_name):
                os.makedirs(dir_name, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(dir=dir_name, text=True)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, snap_file)
            temp_path = None
        except Exception:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def load_daily_snapshot(self, ticker):
        snap_file = self._get_snapshot_file(ticker)
        if os.path.exists(snap_file):
            try:
                with open(snap_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data.get("plan")
            except Exception:
                pass
        return None

    def reset_residual(self, ticker):
        self._load_state_if_needed(ticker)
        self.residual["BUY1"][ticker] = 0.0
        self.residual["BUY2"][ticker] = 0.0
        self.residual["SELL_L1"][ticker] = 0.0
        self.residual["SELL_UPPER"][ticker] = 0.0
        self.residual["SELL_JACKPOT"][ticker] = 0.0
        self.executed["BUY_BUDGET"][ticker] = 0.0
        self.executed["SELL_QTY"][ticker] = 0
        self._save_state(ticker)

    def record_execution(self, ticker, side, qty, exec_price):
        self._load_state_if_needed(ticker)
        # MODIFIED: [실행 기록 TypeError 및 예산 증발 방어] 명시적 형변환(Safe Casting)을 통한 런타임 보호
        safe_qty = int(float(qty or 0))
        safe_price = float(exec_price or 0.0)
        
        if side == "BUY":
            spent = safe_qty * safe_price
            self.executed["BUY_BUDGET"][ticker] = float(self.executed.get("BUY_BUDGET", {}).get(ticker, 0.0)) + spent
        else:
            self.executed["SELL_QTY"][ticker] = int(self.executed.get("SELL_QTY", {}).get(ticker, 0)) + safe_qty
        self._save_state(ticker)

    def get_dynamic_plan(self, ticker, curr_p, prev_c, current_weight, vwap_status, min_idx, alloc_cash, q_data, is_snapshot_mode=False):
        if not is_snapshot_mode:
            cached_plan = self.load_daily_snapshot(ticker)
            if cached_plan:
                return cached_plan

        self._load_state_if_needed(ticker)

        # MODIFIED: [min_idx 결측치 런타임 붕괴 방어] int 캐스팅 및 None 폴백 적용
        min_idx = int(min_idx) if min_idx is not None else -1
        if min_idx < 0 or min_idx >= 30:
            if not vwap_status.get('is_strong_up') and not vwap_status.get('is_strong_down'):
                return {"orders": [], "trigger_loc": False, "total_q": 0}

        # MODIFIED: [무상증자/0달러 로트 평단가 붕괴 방어] 가격이 0 초과인 정상 로트만 연산에 참여하여 ZeroDivision 및 왜곡 차단
        valid_q_data = [item for item in q_data if float(item.get('price', 0.0)) > 0]
        total_q = sum(int(item.get("qty", 0)) for item in valid_q_data)
        total_inv = sum(float(item.get('qty', 0)) * float(item.get('price', 0.0)) for item in valid_q_data)
        avg_price = (total_inv / total_q) if total_q > 0 else 0.0
        
        dates_in_queue = sorted(list(set(item.get('date') for item in valid_q_data if item.get('date'))), reverse=True)
        l1_qty, l1_price = 0, 0.0
        
        if dates_in_queue:
            lots_1 = [item for item in valid_q_data if item.get('date') == dates_in_queue[0]]
            l1_qty = sum(int(item.get('qty', 0)) for item in lots_1)
            l1_price = sum(float(item.get('qty', 0)) * float(item.get('price', 0.0)) for item in lots_1) / l1_qty if l1_qty > 0 else 0.0
            
        upper_qty = total_q - l1_qty
        upper_inv = total_inv - (l1_qty * l1_price)
        upper_avg = upper_inv / upper_qty if upper_qty > 0 else 0.0

        trigger_jackpot = round(avg_price * 1.010, 2)
        trigger_l1 = round(l1_price * 1.006, 2)
        trigger_upper = round(upper_avg * 1.005, 2) if upper_qty > 0 else 0.0

        # NEW: [자전거래 원천 차단 방어막 (Dynamic Shield)] 시스템 상 예정/존재하는 매도 최저가 스캔
        system_sell_triggers = [p for p in [trigger_jackpot, trigger_l1, trigger_upper] if p > 0]
        system_min_sell = min(system_sell_triggers) if system_sell_triggers else 0.0

        def _apply_wash_trade_shield(raw_orders):
            if system_min_sell <= 0:
                return raw_orders
            safe_limit = round(system_min_sell - 0.01, 2)
            filtered = []
            for o in raw_orders:
                if o["side"] == "BUY" and o["price"] >= system_min_sell:
                    if safe_limit > 0:
                        o["price"] = safe_limit
                        filtered.append(o)
                else:
                    filtered.append(o)
            return filtered

        if total_q == 0:
            side = "BUY"
            p1_trigger = round(prev_c / 0.935, 2)
            p2_trigger = round(prev_c * 0.999, 2)
        else:
            side = "SELL" if curr_p > prev_c else "BUY"
            p1_trigger = round(prev_c * 0.995, 2)
            p2_trigger = round(prev_c * 0.9725, 2)

        is_strong_up = vwap_status.get('is_strong_up', False)
        is_strong_down = vwap_status.get('is_strong_down', False)
        trigger_loc = is_strong_up or is_strong_down 

        orders = []

        if trigger_loc or is_snapshot_mode:
            total_spent = float(self.executed["BUY_BUDGET"].get(ticker, 0.0))
            rem_budget = max(0.0, float(alloc_cash) - total_spent)
            if rem_budget > 0:
                b1_budget = rem_budget * 0.5
                b2_budget = rem_budget - b1_budget
                
                q1 = math.floor(b1_budget / p1_trigger) if p1_trigger > 0 else 0
                q2 = math.floor(b2_budget / p2_trigger) if p2_trigger > 0 else 0
                
                if q1 > 0: orders.append({"side": "BUY", "qty": q1, "price": p1_trigger})
                if q2 > 0: orders.append({"side": "BUY", "qty": q2, "price": p2_trigger})
                
                if total_q > 0:
                    max_n = 5
                    if curr_p > 0:
                        required_n = math.ceil(b2_budget / curr_p) - q2
                        if required_n > 5:
                            max_n = min(required_n, 50)
                    
                    for n in range(1, max_n + 1):
                        if (q2 + n) > 0:
                            grid_p2 = round(b2_budget / (q2 + n), 2)
                            if grid_p2 >= 0.01 and grid_p2 < p2_trigger:
                                orders.append({"side": "BUY", "qty": 1, "price": grid_p2})
                
            rem_qty_total = max(0, int(total_q) - int(self.executed["SELL_QTY"].get(ticker, 0)))
            if rem_qty_total > 0:
                if curr_p >= trigger_jackpot:
                    orders.append({"side": "SELL", "qty": rem_qty_total, "price": trigger_jackpot})
                else:
                    # MODIFIED: [상위 레이어 초과 매도 산출 오류 차단] 실제로 1층에서 할당된 수량만 차감하도록 로직 분리
                    available_l1 = min(l1_qty, rem_qty_total)
                    l1_queued = 0
                    if available_l1 > 0 and curr_p >= trigger_l1:
                        orders.append({"side": "SELL", "qty": available_l1, "price": trigger_l1})
                        l1_queued = available_l1
                        
                    available_upper = min(upper_qty, rem_qty_total - l1_queued)
                    if available_upper > 0 and trigger_upper > 0 and curr_p >= trigger_upper:
                        orders.append({"side": "SELL", "qty": available_upper, "price": trigger_upper})
            
            # MODIFIED: [자전거래 방어막 가동] 스냅샷 및 LOC 전송 전 매수 타점 Capping
            orders = _apply_wash_trade_shield(orders)
            
            plan_result = {"orders": orders, "trigger_loc": True, "total_q": total_q}
            
            if is_snapshot_mode:
                self.save_daily_snapshot(ticker, plan_result)
                
            return plan_result

        rem_weight = sum(self.U_CURVE_WEIGHTS[min_idx:])
        slice_ratio_sell = current_weight / rem_weight if rem_weight > 0 else 1.0
        
        total_weight = sum(self.U_CURVE_WEIGHTS)
        slice_ratio_buy = current_weight / total_weight if total_weight > 0 else 1.0

        if side == "BUY":
            total_spent = float(self.executed["BUY_BUDGET"].get(ticker, 0.0))
            if total_spent >= alloc_cash:
                return {"orders": [], "trigger_loc": False, "total_q": total_q}
            
            b1_budget_slice = (alloc_cash * 0.5) * slice_ratio_buy
            b2_budget_slice = (alloc_cash * 0.5) * slice_ratio_buy

            if curr_p > 0 and curr_p <= p1_trigger:
                exact_q1 = (b1_budget_slice / curr_p) + float(self.residual["BUY1"].get(ticker, 0.0))
                alloc_q1 = int(math.floor(exact_q1))
                self.residual["BUY1"][ticker] = float(exact_q1 - alloc_q1)
                if alloc_q1 > 0:
                    orders.append({"side": "BUY", "qty": alloc_q1, "price": p1_trigger})
                    
            if curr_p > 0 and curr_p <= p2_trigger:
                exact_q2 = (b2_budget_slice / curr_p) + float(self.residual["BUY2"].get(ticker, 0.0))
                alloc_q2 = int(math.floor(exact_q2))
                self.residual["BUY2"][ticker] = float(exact_q2 - alloc_q2)
                if alloc_q2 > 0:
                    orders.append({"side": "BUY", "qty": alloc_q2, "price": p2_trigger})

        else: # SELL
            # 🚨 [수술 완료] int 강제 캐스팅으로 소수점 주식 찌꺼기 100% 절단
            rem_qty_total = max(0, int(total_q) - int(self.executed["SELL_QTY"].get(ticker, 0)))
            if rem_qty_total <= 0:
                return {"orders": [], "trigger_loc": False, "total_q": total_q}

            if curr_p >= trigger_jackpot:
                exact_qs = float(total_q * slice_ratio_sell) + float(self.residual["SELL_JACKPOT"].get(ticker, 0.0))
                alloc_qs = int(min(math.floor(exact_qs), rem_qty_total))
                self.residual["SELL_JACKPOT"][ticker] = float(exact_qs - alloc_qs)
                if alloc_qs > 0:
                    orders.append({"side": "SELL", "qty": alloc_qs, "price": trigger_jackpot})
            
            else:
                if l1_qty > 0 and curr_p >= trigger_l1:
                    exact_l1 = float(l1_qty * slice_ratio_sell) + float(self.residual["SELL_L1"].get(ticker, 0.0))
                    alloc_l1 = int(min(math.floor(exact_l1), rem_qty_total))
                    self.residual["SELL_L1"][ticker] = float(exact_l1 - alloc_l1)
                    if alloc_l1 > 0:
                        orders.append({"side": "SELL", "qty": alloc_l1, "price": trigger_l1})
                        rem_qty_total -= alloc_l1

                if upper_qty > 0 and trigger_upper > 0 and curr_p >= trigger_upper and rem_qty_total > 0:
                    exact_upper = float(upper_qty * slice_ratio_sell) + float(self.residual["SELL_UPPER"].get(ticker, 0.0))
                    alloc_upper = int(min(math.floor(exact_upper), rem_qty_total))
                    self.residual["SELL_UPPER"][ticker] = float(exact_upper - alloc_upper)
                    if alloc_upper > 0:
                        orders.append({"side": "SELL", "qty": alloc_upper, "price": trigger_upper})

        # MODIFIED: [자전거래 방어막 가동] VWAP 실시간 타격 전 매수 타점 Capping
        orders = _apply_wash_trade_shield(orders)

        self._save_state(ticker)
        return {"orders": orders, "trigger_loc": False, "total_q": total_q}
