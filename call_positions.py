"""Authenticated BB12 Call position tracker. No brokerage execution or background alerts."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import re
import requests
import pandas as pd
import streamlit as st
import yfinance as yf

ET=ZoneInfo("America/New_York")


def api(base,key,method,*,params=None,payload=None):
    headers={"apikey":key,"Authorization":f"Bearer {key}","Content-Type":"application/json",
             "Accept":"application/json","Prefer":"return=representation"}
    response=requests.request(method,f"{base}/rest/v1/bb12_call_positions",headers=headers,
                              params=params,json=payload,timeout=20)
    if not response.ok:
        raise RuntimeError(f"数据库HTTP {response.status_code}：{response.text[:200]}")
    return response.json() if response.content else []


@st.cache_data(ttl=300,show_spinner=False)
def yahoo_bid(ticker,expiration,strike):
    """Exact strike/expiration CALL; never silently use last/ask as executable bid."""
    obj=yf.Ticker(ticker)
    if expiration not in obj.options:
        return {"bid":None,"note":"Yahoo未列出该到期日", "last_trade":""}
    calls=obj.option_chain(expiration).calls
    matches=calls.loc[(calls['strike'].astype(float)-float(strike)).abs()<0.0001]
    if matches.empty:
        return {"bid":None,"note":"Yahoo未列出该行权价", "last_trade":""}
    row=matches.iloc[0]
    bid=float(row.get('bid',0) or 0)
    ask=float(row.get('ask',0) or 0)
    trade=str(row.get('lastTradeDate',''))
    if not (bid>0 and ask>=bid):
        return {"bid":None,"note":"Bid缺失、为0或报价异常，请手动核对IBKR", "last_trade":trade}
    return {"bid":bid,"note":"Yahoo报价可能延迟或过时；请在IBKR确认", "last_trade":trade}


def calc_alert(pos,bid,today):
    days=(today-pos['opened_on']).days
    days_left=(pos['expiration']-today).days
    gain=(bid/pos['entry_premium']-1)*100 if bid is not None else None
    # B: within first 30 calendar days 50%; afterwards 100%.
    target=50 if pos['strategy']=='B' and days<=30 else 100
    profit= gain is not None and gain>=target
    risk=days_left<=30
    return gain,target,profit,risk,days_left


def render_call_positions(base,key):
    st.divider()
    st.header('📒 我的Call持仓与止盈提醒')
    st.caption('人工录入IBKR成交；Yahoo报价优先，失败时手动输入IBKR Bid。仅打开网页并点击检查时更新，不后台推送、不自动下单。')
    with st.expander('➕ 记录一笔新的Call开仓',expanded=False):
        with st.form('call_add',clear_on_submit=True):
            c1,c2=st.columns(2)
            with c1:
                ticker=st.text_input('股票代码',placeholder='例如 V').strip().upper()
                expiry=st.date_input('到期日',value=date.today()+timedelta(days=180))
                strike=st.number_input('行权价（美元）',min_value=0.01,value=100.0,step=1.0)
                strategy=st.selectbox('止盈规则',['A','B'],format_func=lambda x:'A：+100%止盈' if x=='A' else 'B：30天内+50%，否则+100%')
            with c2:
                opened=st.date_input('实际买入日期',value=date.today())
                entry=st.number_input('实际买入权利金（每股美元）',min_value=0.01,value=5.0,step=0.1)
                qty=st.number_input('合约数量（每张100股）',min_value=1,value=1,step=1)
            submit=st.form_submit_button('保存到Supabase',type='primary')
        if submit:
            try:
                if not re.fullmatch(r'[A-Z0-9^][A-Z0-9.^=-]{0,19}',ticker):
                    raise ValueError('股票代码格式错误')
                if opened>date.today() or expiry<opened:
                    raise ValueError('买入日期不能晚于今天或到期日')
                api(base,key,'POST',payload={'ticker':ticker,'expiration':expiry.isoformat(),
                    'strike':float(strike),'opened_on':opened.isoformat(),
                    'entry_premium':float(entry),'contracts':int(qty),'strategy':strategy})
                st.success('已保存开仓记录。请点击下方“刷新持仓与检查报价”。')
            except Exception as exc: st.error(f'保存失败：{exc}')

    if st.button('🔄 刷新持仓与检查Yahoo期权Bid',type='primary',key='refresh_calls'):
        yahoo_bid.clear()
        st.session_state['call_check']=True
    try:
        raw=api(base,key,'GET',params={'select':'*','order':'id.desc','limit':'500'})
    except Exception as exc:
        st.error(f'无法读取Call持仓表：{exc}。请先执行升级包中的 supabase_calls_setup.sql。')
        return
    positions=[]
    for r in raw:
        try:
            r['opened_on']=date.fromisoformat(r['opened_on']);r['expiration']=date.fromisoformat(r['expiration'])
            positions.append(r)
        except Exception: continue
    active=[p for p in positions if p['status']=='OPEN']
    st.write(f'云端记录：**{len(active)}笔未平仓**，**{len(positions)-len(active)}笔已平仓**。')
    if not active:
        st.info('暂无未平仓Call。先录入实际成交信息。')
    else:
        checked=bool(st.session_state.get('call_check',False))
        if not checked: st.info('请点击“刷新持仓与检查Yahoo期权Bid”获取报价。')
        for p in active:
            title=f"#{p['id']} · {p['ticker']} {p['expiration']} C {float(p['strike']):g} · {p['strategy']}"
            with st.expander(title,expanded=True):
                today=datetime.now(ET).date()
                bid=None; note='尚未检查报价'; trade=''
                if checked:
                    try:
                        q=yahoo_bid(p['ticker'],p['expiration'].isoformat(),float(p['strike']))
                        bid=q['bid'];note=q['note'];trade=q['last_trade']
                    except Exception as exc: note=f'Yahoo获取失败：{str(exc)[:120]}'
                st.caption(f"买入 {p['opened_on']} · 成本 ${float(p['entry_premium']):.2f}/股 · {p['contracts']}张 · Yahoo: {note}")
                if trade: st.caption(f'Yahoo最近成交时间（不是Bid时间）：{trade}')
                manual=st.number_input('IBKR当前Bid（美元/股；0表示不用手动报价）',min_value=0.0,
                    value=0.0,step=0.05,key=f"manual_bid_{p['id']}")
                effective=manual if manual>0 else bid
                gain,target,profit,risk,left=calc_alert(p,effective,today)
                if effective is None:
                    st.warning('没有有效Bid：无法判断是否止盈。请在IBKR核对并手动填写Bid。')
                else:
                    source='手动IBKR Bid' if manual>0 else 'Yahoo Bid'
                    st.write(f"**{source}：${effective:.2f}** · 估算收益 **{gain:+.1f}%** · 当前止盈阈值 **+{target}%**")
                    if profit: st.error('🎯 已达到止盈阈值：请立即在IBKR核对可成交报价，人工决定是否平仓。')
                    else: st.info('尚未达到止盈阈值。')
                if risk: st.warning(f'⏳ 距到期还有 {left} 天（≤30天），请检查时间价值和到期风险。')
                if left<0: st.error('该合约已过到期日，请核对是否到期、行权或需要更新记录。')
                with st.form(f"close_{p['id']}"):
                    close_day=st.date_input('实际平仓日期',value=today,key=f"close_day_{p['id']}")
                    close_price=st.number_input('实际平仓成交权利金（美元/股）',min_value=0.0,
                        value=0.0,step=0.05,key=f"close_price_{p['id']}")
                    close_submit=st.form_submit_button('确认已经在IBKR平仓，归档记录')
                if close_submit:
                    try:
                        if close_day<p['opened_on'] or close_day>today: raise ValueError('平仓日期无效')
                        result=api(base,key,'PATCH',params={'id':f"eq.{p['id']}",'status':'eq.OPEN'},
                          payload={'status':'CLOSED','closed_on':close_day.isoformat(),'close_premium':float(close_price)})
                        if len(result)!=1: raise RuntimeError('持仓状态已改变，请刷新')
                        st.session_state.pop('call_check',None)
                        st.success('已归档。请刷新页面。')
                        st.rerun()
                    except Exception as exc: st.error(f'归档失败：{exc}')
    closed=[p for p in positions if p['status']=='CLOSED']
    with st.expander(f'📁 已平仓记录（{len(closed)}）'):
        if closed:
            rows=[]
            for p in closed:
                exit_price=float(p['close_premium'] or 0); entry=float(p['entry_premium'])
                rows.append({'ID':p['id'],'代码':p['ticker'],'到期':str(p['expiration']),
                    '策略':p['strategy'],'开仓':str(p['opened_on']),'平仓':p.get('closed_on'),
                    '成本':entry,'卖出':exit_price,'已实现盈亏(美元，未扣费用)':round((exit_price-entry)*100*p['contracts'],2)})
            st.dataframe(pd.DataFrame(rows),use_container_width=True,hide_index=True)
        else: st.caption('暂无已平仓记录')
    st.caption('注意：Yahoo期权链可能延迟、缺失或报价陈旧；网页检查不等于盘中触价提醒。策略B的30天按日历天计算。所有提醒仅供人工核对。')
