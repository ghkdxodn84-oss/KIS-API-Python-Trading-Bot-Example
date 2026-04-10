# ==========================================================
# [strategy_v_avwap.py] 
# 💡 V-REV 하이브리드 전용 차세대 AVWAP 스나이퍼 플러그인 (Dual-Referencing)
# ⚠️ 초공격형 당일 청산 암살자 (V-REV 잉여 현금 100% 몰빵 & -3% 하드스탑)
# ⚠️ 옵션 B 아키텍처: 기초자산(SOXX) 시그널 스캔 + 파생상품(SOXL) 미시구조 타격
# ==========================================================
import logging
import datetime
import pytz
import math
import yfinance as yf
import pandas as pd

class VAvwapHybridPlugin:
    def __init__(self):
        self.plugin_name = "AVWAP_HYBRID_DUAL"
        # MODIFIED: 기초자산(SOXX) 펀더멘털 스케일로 파라미터 변환 (3배수 레버리지 역산)
        self.leverage = 3.0             # 투영(Projection)을 위한 레버리지 배수
        self.base_stop_loss_pct = 0.01  # SOXX 기준 -1% (SOXL -3% 상당 하드스탑)
        self.base_target_pct = 0.01     # SOXX 기준 +1% (SOXL +3% 상당 스퀴즈 익절)
        self.base_dip_buy_pct = 0.0067  # SOXX 기준 -0.67% (SOXL -2% 상당 VWAP 바운스)
        
    def fetch_macro_context(self, base_ticker):
        """
        [Pre-Fetch] 야후 파이낸스를 통해 기초자산(SOXX)의 과거 20일 20MA 및 30분봉(09:30) 평균 거래량 추출
        매일 장 초반 단 1회만 호출되어 메모리에 캐싱됩니다.
        """
        # MODIFIED: ticker 변수명을 기초자산 명확성을 위해 base_ticker로 변경
        try:
            tkr = yf.Ticker(base_ticker)
            # 1. 20MA 추출용 일봉 데이터
            df_daily = tkr.history(period="2mo", interval="1d", timeout=5)
            # 2. RVOL 추출용 30분봉 데이터
            df_30m = tkr.history(period="60d", interval="30m", timeout=5)

            if df_daily.empty or len(df_daily) < 20 or df_30m.empty:
                return None

            prev_close = float(df_daily['Close'].iloc[-2])
            ma_20 = float(df_daily['Close'].rolling(window=20).mean().iloc[-2])

            # 타임존 보정 (US/Eastern)
            if df_30m.index.tz is None:
                df_30m.index = df_30m.index.tz_localize('UTC').tz_convert('US/Eastern')
            else:
                df_30m.index = df_30m.index.tz_convert('US/Eastern')

            # 09:30:00 캔들만 필터링
            first_30m = df_30m[df_30m.index.time == datetime.time(9, 30)]
            
            # 당일 09:30 캔들이 실시간으로 끼어있을 수 있으므로 과거 데이터만 추출
            today_est = datetime.datetime.now(pytz.timezone('US/Eastern')).date()
            past_first_30m = first_30m[first_30m.index.date < today_est]
            
            if len(past_first_30m) >= 20:
                avg_vol_20 = float(past_first_30m['Volume'].tail(20).mean())
            elif len(past_first_30m) > 0:
                avg_vol_20 = float(past_first_30m['Volume'].mean())
            else:
                avg_vol_20 = 0.0

            return {
                "prev_close": prev_close,
                "ma_20": ma_20,
                "avg_vol_20": avg_vol_20
            }
            
        except Exception as e:
            logging.error(f"🚨 [V_AVWAP] YF 기초자산 매크로 컨텍스트 추출 실패 ({base_ticker}): {e}")
            return None

    # MODIFIED: 듀얼 레퍼런싱을 위해 base(SOXX)와 exec(SOXL) 파라미터로 이원화
    def get_decision(self, base_ticker, exec_ticker, base_curr_p, exec_curr_p, base_day_open, avwap_avg_price, avwap_qty, avwap_alloc_cash, context_data, df_1min_base, now_est):
        """
        실시간 기초자산(SOXX) 데이터를 기반으로 V-Shape 암살자의 다음 행동을 결정하고 파생상품(SOXL) 호가로 타격합니다.
        ⚠️ 주의: 여기서 파라미터로 받는 avwap_qty와 avwap_avg_price는 V-REV의 물량이 완벽히 배제된 순수 AVWAP 전용 수치여야 합니다.
        """
        curr_time = now_est.time()
        
        # 기본 시간 통제선
        time_0930 = datetime.time(9, 30)
        time_1000 = datetime.time(10, 0)
        time_1400 = datetime.time(14, 0)
        time_1430 = datetime.time(14, 30)
        time_1555 = datetime.time(15, 55)

        # --------------------------------------------------------
        # 1. KIS 1분봉 데이터 기반 당일 기초자산(SOXX) VWAP 및 초반 30분 누적 거래량 동적 연산
        # --------------------------------------------------------
        # MODIFIED: 연산의 기준을 모두 기초자산(base_curr_p, df_1min_base)으로 동기화
        base_vwap = base_curr_p
        base_current_30m_vol = 0.0
        
        if df_1min_base is not None and not df_1min_base.empty:
            try:
                df = df_1min_base.copy()
                # KIS API 표준 컬럼명 추종 연산
                df['tp'] = (df['stck_hgpr'].astype(float) + df['stck_lwpr'].astype(float) + df['stck_prpr'].astype(float)) / 3.0
                df['vol'] = df['cntg_vol'].astype(float)
                df['vol_tp'] = df['tp'] * df['vol']
                
                cum_vol = df['vol'].sum()
                cum_vol_tp = df['vol_tp'].sum()
                base_vwap = cum_vol_tp / cum_vol if cum_vol > 0 else base_curr_p
                
                # 09:30 ~ 10:00 (EST) 거래량 스캔 (KIS 'stck_cntg_hour' 필드 사용)
                mask_30m = (df['stck_cntg_hour'] >= '093000') & (df['stck_cntg_hour'] < '100100')
                base_current_30m_vol = df.loc[mask_30m, 'vol'].sum()
            except Exception as e:
                logging.debug(f"[V_AVWAP] 기초자산 1분봉 파싱 에러 (기본값 대체): {e}")

        # --------------------------------------------------------
        # 2. 보유 중일 때의 3중 청산 시퀀스 (Exit & Risk Management)
        # --------------------------------------------------------
        if avwap_qty > 0:
            # ① [하드스탑] 펀더멘털 스케일 손절 (SOXL의 손익률을 기초자산 비율로 역산하여 노이즈 방어)
            # MODIFIED: 파생상품의 변동성 끌림을 배제한 순수 기초자산 손실률 기반 스탑로스
            exec_return = (exec_curr_p - avwap_avg_price) / avwap_avg_price
            base_equivalent_return = exec_return / self.leverage
            
            if base_equivalent_return <= -self.base_stop_loss_pct:
                return {'action': 'SELL', 'qty': avwap_qty, 'target_price': 0.0, 'reason': 'HARD_STOP_DUAL'}
            
            # ② [타임스탑] 15:55 EST 도달 시 전량 청산 (장 마감 덤핑 회피)
            if curr_time >= time_1555:
                return {'action': 'SELL', 'qty': avwap_qty, 'target_price': 0.0, 'reason': 'TIME_STOP'}
                
            # ③ [스퀴즈 익절] 14:30 이후 기초자산(SOXX)이 당일 VWAP 대비 목표치 도달 시 홈런 익절
            # MODIFIED: 기초자산 VWAP 돌파 여부로 스퀴즈 판별
            if curr_time >= time_1430 and base_curr_p >= base_vwap * (1 + self.base_target_pct):
                return {'action': 'SELL', 'qty': avwap_qty, 'target_price': 0.0, 'reason': 'SQUEEZE_TARGET_DUAL'}
                
            return {'action': 'HOLD', 'reason': '보유중_관망', 'vwap': base_vwap}

        # --------------------------------------------------------
        # 3. 신규 진입 시퀀스 (AVWAP 단독 보유 물량 0주)
        # --------------------------------------------------------
        if not context_data:
            return {'action': 'WAIT', 'reason': '매크로_데이터_수집대기', 'vwap': base_vwap}

        prev_c = context_data['prev_close']
        ma_20 = context_data['ma_20']
        avg_vol_20 = context_data['avg_vol_20']

        # ① [상승장 필터] 기초자산의 시가 및 전일 종가가 20MA 상단인지 확인
        # MODIFIED: 기초자산(base_day_open) 펀더멘털 20MA 상회 여부
        is_bull_regime = (prev_c > ma_20) and (base_day_open > ma_20)
        if not is_bull_regime:
            return {'action': 'SHUTDOWN', 'reason': '기초자산_역배열_하락장_영구동결', 'vwap': base_vwap}
            
        # ② [갭하락 필터] 기초자산 시가가 전일 종가 대비 임계치 이하일 경우 동결
        # MODIFIED: SOXX 스케일의 갭하락 기준 적용
        if base_day_open <= prev_c * (1 - self.base_dip_buy_pct):
            return {'action': 'SHUTDOWN', 'reason': '기초자산_시가_갭하락_영구동결', 'vwap': base_vwap}
            
        # ③ [구조적 붕괴 RVOL 필터] 10:00 EST 시점 기초자산 거래량 폭발 판단
        if curr_time >= time_1000:
            if avg_vol_20 > 0 and base_current_30m_vol >= (avg_vol_20 * 2.0) and base_curr_p < base_vwap:
                return {'action': 'SHUTDOWN', 'reason': '기초자산_RVOL_스파이크_영구동결', 'vwap': base_vwap}
                
        # ④ [핀포인트 진입] 10:00 ~ 14:00 사이 기초자산이 당일 VWAP 대비 이격도 도달 시 파생상품(SOXL) 매수
        if time_1000 <= curr_time <= time_1400:
            # MODIFIED: SOXX 가격이 앵커 도달 시 SOXL 현재가(exec_curr_p)로 물량 산정 후 100% 타격
            if base_curr_p <= base_vwap * (1 - self.base_dip_buy_pct):
                buy_qty = math.floor(avwap_alloc_cash / exec_curr_p)
                if buy_qty > 0:
                    return {'action': 'BUY', 'qty': buy_qty, 'target_price': exec_curr_p, 'reason': 'VWAP_BOUNCE_DUAL', 'vwap': base_vwap}
                else:
                    return {'action': 'WAIT', 'reason': '예산_부족_관망', 'vwap': base_vwap}
                    
        return {'action': 'WAIT', 'reason': '타점_대기중', 'vwap': base_vwap}

