# ... (기존 장중 BUY / SELL 로직 완료 후) ...
        
        # [수정 제안] 함수 맨 마지막 self._save_state(ticker) 직전에 공통 방어막 가동
        sell_prices = [o["price"] for o in orders if o["side"] == "SELL"]
        if sell_prices:
            min_sell_price = min(sell_prices)
            safe_orders = []
            for o in orders:
                if o["side"] == "BUY" and o["price"] >= min_sell_price:
                    continue # 자전거래 소각
                safe_orders.append(o)
            orders = safe_orders

        self._save_state(ticker)
        return {"orders": orders, "trigger_loc": False, "total_q": total_q}
