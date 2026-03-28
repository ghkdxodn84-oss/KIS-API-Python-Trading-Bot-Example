# ==========================================================
# [volatility_engine.py]
# ==========================================================
import yfinance as yf
import pandas as pd
import numpy as np
import os
import json
import tempfile

CACHE_FILE = "data/volatility_cache.json"

def _load_cache(key, default_val):
    """ 🛡️ 통신 장애 시 직전 영업일의 1년 평균값을 로드하는 1차 방어막 """
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r') as f:
                data = json.load(f)
                val = data.get(key)
                if val is not None and float(val) > 0:
                    return float(val)
        except Exception:
            pass
    # 최후의 보루(콜드스타트) 2차 방어막 반환
    return default_val

def _save_cache(key, value):
    """ 🛡️ 원자적 쓰기(fsync)를 통해 무결성이 보장된 로컬 캐시 저장 """
    data = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r') as f:
                data = json.load(f)
        except Exception:
            pass
    
    data[key] = value
    
    try:
        dir_name = os.path.dirname(CACHE_FILE)
        if dir_name and not os.path.exists(dir_name):
            os.makedirs(dir_name, exist_ok=True)
            
        fd, temp_path = tempfile.mkstemp(dir=dir_name, text=True)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f)
            f.flush()
            os.fsync(fd)
        os.replace(temp_path, CACHE_FILE)
    except Exception as e:
        print(f"⚠️ [Engine] 1년 평균 캐시 저장 실패: {e}")

def get_tqqq_target_drop():
    """ [ TQQQ 스나이퍼 ] 실시간 VXN을 최근 1년 평균으로 나누어 타격선을 계산합니다. """
    try:
        # 💡 [V3.0+ 수술] 5일치 대신 1년(1y) 데이터를 호출하여 현재값과 1년 평균을 동시 추출
        vxn_data = yf.download("^VXN", period="1y", interval="1d", progress=False)
        
        if vxn_data.empty: 
            return -6.18
            
        if hasattr(vxn_data.columns, 'droplevel'):
            if isinstance(vxn_data.columns, pd.MultiIndex):
                vxn_data.columns = vxn_data.columns.droplevel(1)
                
        valid_closes = vxn_data['Close'].dropna()
        if valid_closes.empty:
            return -6.18
            
        current_vxn = float(valid_closes.iloc[-1])
        
        # 💡 [롤링 1년 평균 연산 및 캐싱]
        try:
            mean_vxn = float(valid_closes.mean())
            if pd.isna(mean_vxn) or mean_vxn <= 0:
                raise ValueError("Invalid Mean")
            _save_cache("VXN_MEAN", mean_vxn)
        except Exception:
            mean_vxn = _load_cache("VXN_MEAN", 20.0)
        
        # 비중 연산 및 타격선 도출
        weight = current_vxn / mean_vxn
        target_drop = round(-6.18 * weight, 2)
        
        return target_drop
        
    except Exception as e:
        print(f"❌ VXN 스캔 오류: {e}")
        return -6.18

def get_soxl_target_drop():
    """ [ SOXL 스나이퍼 ] SOXX HV를 최근 1년 평균 HV로 나누어 타격선 계산 """
    try:
        # 💡 [V3.0+ 수술] 1년치 데이터를 당겨와 전체 구간의 20일 HV 시계열을 생성
        soxx_data = yf.download("SOXX", period="1y", interval="1d", progress=False)
        if soxx_data.empty or len(soxx_data) < 21: 
            return -7.59
        
        if hasattr(soxx_data.columns, 'droplevel'):
            if isinstance(soxx_data.columns, pd.MultiIndex):
                soxx_data.columns = soxx_data.columns.droplevel(1)
                
        closes = soxx_data['Close'].dropna()
        log_returns = np.log(closes / closes.shift(1))
        hv_20d = log_returns.rolling(window=20).std() * np.sqrt(252) * 100
        
        valid_hvs = hv_20d.dropna()
        if valid_hvs.empty:
            return -7.59
            
        latest_hv = float(valid_hvs.iloc[-1])
        
        # 💡 [롤링 1년 평균 연산 및 캐싱]
        try:
            mean_hv = float(valid_hvs.mean())
            if pd.isna(mean_hv) or mean_hv <= 0:
                raise ValueError("Invalid Mean")
            _save_cache("SOXX_HV_MEAN", mean_hv)
        except Exception:
            mean_hv = _load_cache("SOXX_HV_MEAN", 25.0)
        
        weight = latest_hv / mean_hv
        return round(-7.59 * weight, 2)
        
    except Exception as e:
        print(f"❌ SOXX HV 연산 오류: {e}")
        return -7.59

def get_tqqq_target_drop_full():
    """ 💡 [텔레그램 UI 표시용] TQQQ 상세 데이터 세트 반환 """
    try:
        vxn_data = yf.download("^VXN", period="1y", interval="1d", progress=False)
        
        if vxn_data.empty: 
            return 0.0, 1.0, -6.18
            
        if hasattr(vxn_data.columns, 'droplevel'):
            if isinstance(vxn_data.columns, pd.MultiIndex):
                vxn_data.columns = vxn_data.columns.droplevel(1)
                
        valid_closes = vxn_data['Close'].dropna()
        if valid_closes.empty:
            return 0.0, 1.0, -6.18
            
        current_vxn = float(valid_closes.iloc[-1])
        
        try:
            mean_vxn = float(valid_closes.mean())
            if pd.isna(mean_vxn) or mean_vxn <= 0:
                raise ValueError("Invalid Mean")
            _save_cache("VXN_MEAN", mean_vxn)
        except Exception:
            mean_vxn = _load_cache("VXN_MEAN", 20.0)
            
        weight = current_vxn / mean_vxn
        target_drop = round(-6.18 * weight, 2)
        
        return current_vxn, weight, target_drop
        
    except Exception as e:
        print(f"❌ VXN 상세 스캔 오류: {e}")
        return 0.0, 1.0, -6.18

def get_soxl_target_drop_full():
    """ 💡 [텔레그램 UI 표시용] SOXL 상세 데이터 세트 반환 """
    try:
        soxx_data = yf.download("SOXX", period="1y", interval="1d", progress=False)
        if soxx_data.empty or len(soxx_data) < 21: 
            return 0.0, 1.0, -7.59
        
        if hasattr(soxx_data.columns, 'droplevel'):
            if isinstance(soxx_data.columns, pd.MultiIndex):
                soxx_data.columns = soxx_data.columns.droplevel(1)
                
        closes = soxx_data['Close'].dropna()
        log_returns = np.log(closes / closes.shift(1))
        hv_20d = log_returns.rolling(window=20).std() * np.sqrt(252) * 100
        
        valid_hvs = hv_20d.dropna()
        if valid_hvs.empty:
            return 0.0, 1.0, -7.59
            
        latest_hv = float(valid_hvs.iloc[-1])
        
        try:
            mean_hv = float(valid_hvs.mean())
            if pd.isna(mean_hv) or mean_hv <= 0:
                raise ValueError("Invalid Mean")
            _save_cache("SOXX_HV_MEAN", mean_hv)
        except Exception:
            mean_hv = _load_cache("SOXX_HV_MEAN", 25.0)
        
        weight = latest_hv / mean_hv
        target_drop = round(-7.59 * weight, 2)
        
        return latest_hv, weight, target_drop
        
    except Exception as e:
        print(f"❌ SOXX HV 상세 연산 오류: {e}")
        return 0.0, 1.0, -7.59
