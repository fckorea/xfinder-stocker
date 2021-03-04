#-*- coding: utf-8 -*-
#-------------------------------------------------------------------------------
# Name:        Stocker v2
# Purpose:
# Python version: 3.7.3
#
# Author:    fckorea
#
# Created:    2021-02-01
# (c) fckorea 2021
#-------------------------------------------------------------------------------

import os
from optparse import OptionParser
import logging
import logging.handlers
import json
import traceback
import time
from datetime import datetime
from datetime import timedelta
import math
from functools import reduce
import re
import requests

from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QObject
from PyQt5.QtCore import QThread
from PyQt5.QtCore import QEventLoop
from PyQt5.QtWidgets import QApplication

from koapy import KiwoomOpenApiPlusEntrypoint
import pandas as pd
from pandas import Timestamp

from trading_calendars import get_calendar

PROG_NAME = 'Stocker'
PROG_VER = '2.0'
LOGGER = None
LOG_DIR = './logs'
LOG_FILENAME = os.path.abspath('%s/%s.log' % (LOG_DIR, PROG_NAME.replace(' ', '-').lower()))
STOCKER_URL = 'http://tbx.kr/api/v1/trader/consensus'
CONFIG = {}

STOCKER_OPTION = {
  # 1: 1day 1trading, 2: realtime trading
  'mode': 1,
  'realtime_interval': 10
}
SYSTEM_OPTION = {
  'auto_shutdown': False
}
CONNECTION_OPTION = {
  'waiting': 600,
  'try_count': 3
}
KIWOOM_OPTION = {
  'money_per_buy': 250000
}
BUY_OPTION = {
  'level': [ 0 ],
  'level_option': [
    { "rate": 4 },
    { "rate": 4 },
    { "rate": 4, "gap": 0.3 }
  ]
}
SELL_OPTION = {
  'static': {
    'enabled': True,
    'percentage': 0.4
  },
  'stats': {
    'enabled': True,
    'days': 60
  },
  'target_price': {
    'enabled': False
  },
  'no_more_buy': {
    'enabled': False,
    'percentage': 0.05
  },
  'speed_mode': {
    'enabled': False,
    'percentage': 0.02
  },
  'minimum_percentage': 0.1
}
TELEGRAM_OPTION = {
  'enabled': False
}

WORKER = {
  'buy': None,
  'sell': None
}

# Today's Signal origin
SIGNAL_LIST = {
  'buy': [],
  'sell': []
}
# Today's buy list (remove duplicate, holding stocks)
BUY_TRADE_LIST = {
  'start_idx': 0,
  'list': []
}
# Today's trading list (from OrderWorker)
TODAY_TRADE_LIST = {
  'buy': [],
  'sell': []
}

KOAPY_TRADER = None

KRX_CALENDAR = get_calendar('XKRX')

#=============================== Worker Class ===============================#
class OrderWorker(QThread):
  orderInfo = None

  def __init__(self, argOrderInfo, parent=None):
      super().__init__(parent=parent)
      # request_name, screen_no, account_no, order_type, code, quantity, price, quote_type, original_order_no
      # [{
      #   'order': {
      #     'req_name': 'STOCKER_BUY_ORDER',
      #     'account_no': account_no,
      #     'order_type': 1,
      #     'symbol_code': '005930',
      #     'quantity': 2,
      #     'price': 0,
      #     'quote_type': '03',
      #     'original_order_no': ''
      #   },
      #   'info': {
      #     'symbol_code': '005930',
      #     'name': '삼성전자',
      #     'market': 'KOSPI',
      #     'market_rank': 1,
      #     'level': 0
      #   }
      # }]
      self.orderInfo = argOrderInfo

  def run(self):
    global LOGGER
    global TODAY_TRADE_LIST
    global KOAPY_TRADER

    LOGGER.debug(self.orderInfo['order'])

    for event in KOAPY_TRADER.OrderCall(self.orderInfo['order']['req_name'], '7777', self.orderInfo['order']['account_no'], self.orderInfo['order']['order_type'], self.orderInfo['order']['symbol_code'], self.orderInfo['order']['quantity'], self.orderInfo['order']['price'], self.orderInfo['order']['quote_type'], self.orderInfo['order']['original_order_no']):
      res = { 'name': event.name }
      for name, value in zip(event.single_data.names, event.single_data.values):
        res[name] = value
      
      LOGGER.debug('=' * 20)
      LOGGER.debug(self.orderInfo['info']['name'])
      LOGGER.debug(res)
      if res['name'] == 'OnReceiveChejanData':
        if '주문상태' in res:
          LOGGER.debug('%s: %s, %s, %s' % (res['주문상태'], res['종목명'], res['체결량'] if '체결량' in res else 'None', res['미체결수량'] if '미체결수량' in res else 'None'))
        else:
          LOGGER.debug('Maybe done!')
          LOGGER.debug('=' * 20)
          break
      LOGGER.debug('=' * 20)
    
    TODAY_TRADE_LIST['buy' if self.orderInfo['order']['order_type'] == 1 else 'sell'].append(self.orderInfo)
    LOGGER.debug('Done %s %s' % ('Buy' if self.orderInfo['order']['order_type'] == 1 else 'Sell', self.orderInfo['info']['name']))

class SellWorker(QThread):
  def run(self):
    global LOGGER
    global CONNECTION_OPTION
    global KIWOOM_OPTION

    while True:
      # if fnCheckOpenKRX():
      if True:  # TEST
        LOGGER.info('<<<<< CHECK SELL >>>>>')
        holding_stocks = fnGetHoldingStocks(KIWOOM_OPTION['account_number'])
        holding_stocks['Market'] = None
        holding_stocks['MarketRank'] = None
        holding_stocks['TargetPrice'] = None
        time.sleep(0.1)
        for more_info in fnGetMoreInfoMyStock(holding_stocks['종목코드']):
          idx = holding_stocks[(holding_stocks['종목코드'] == more_info['symbol_code'])].index
          if len(idx) == 1:
            idx = idx[0]
            holding_stocks.loc[idx, 'Market'] = more_info['market']
            holding_stocks.loc[idx, 'MarketRank'] = more_info['market_rank']
            holding_stocks.loc[idx, 'TargetPrice'] = more_info['target_price']
        
        time.sleep(0.1)
        # TEST
        holding_stocks.loc[2, 'TargetPrice'] = 50000
        SELL_OPTION['stats']['percentage']['KOSDAQ']['avg_profit_rate'] = 0.01
        # End of TEST
        
        print(holding_stocks.to_markdown())
        sell_list = fnCheckSellStocks(holding_stocks)
        print(sell_list)

        LOGGER.info('<<<<< SELL TRADING >>>>>')
        LOGGER.info('Sell %d stocks' % (len(sell_list)))

        for idx in sell_list:
          stocks = sell_list[idx]
          print(idx)
          print(stocks)
          print(type(stocks['info']['종목코드']), stocks['info']['종목코드'])
          print(type(stocks['info']['매매가능수량']), stocks['info']['매매가능수량'])
          # request_name = '삼성전자 1주 시장가 신규 매도' # 사용자 구분명, 구분가능한 임의의 문자열
          # screen_no = '0001'                           # 화면번호
          # account_no = first_account_no                # 계좌번호 10자리, 여기서는 계좌번호 목록에서 첫번째로 발견한 계좌번호로 매수처리
          # order_type = 2         # 주문유형, 2 : 신규매도
          # code = samsung_code    # 종목코드, 앞의 삼성전자 종목코드
          # quantity = 1           # 주문수량, 1주 매도
          # price = 0              # 주문가격, 시장가 매도는 가격설정 의미없음
          # quote_type = '03'      # 거래구분, 03 : 시장가
          # original_order_no = '' # 원주문번호, 주문 정정/취소 등에서 사용
          # context.OrderCall(request_name, screen_no, account_no, order_type, code, quantity, price, quote_type, original_order_no)
          for event in KOAPY_TRADER.OrderCall('STOCKER_SELL_ORDER', '7777', KIWOOM_OPTION['account_number'], 2, stocks['info']['종목코드'], stocks['info']['매매가능수량'], 0, '03', ''):
            LOGGER.debug(event)

        # if STOCKER_OPTION['mode'] == 1:
        #   break
        break
      else:
        LOGGER.info('KRX closed!')
        LOGGER.info('SELL WORKER TERMINATE')
        time.sleep(1)
        break
      time.sleep(1)
    
    return

class BuyWorker(QThread):
  def run(self):
    global LOGGER
    global STOCKER_OPTION
    global CONNECTION_OPTION
    global KIWOOM_OPTION
    global BUY_TRADE_LIST
    global WORKER
    global KOAPY_TRADER

    if STOCKER_OPTION['mode'] == 1:
      LOGGER.info('Mode: 1 day 1 trading')
      LOGGER.info('Wait Sell Worker Terminate')
      while True:
        if WORKER['sell'].isRunning() is False:
          break
        time.sleep(1)
    
    while True:
      # if fnCheckOpenKRX():
      if True:  # TEST
        LOGGER.info('<<<<< CHECK BUY >>>>>')

        buy_list = fnCheckBuyStocks()

        LOGGER.debug('Buy list (%d)' % (len(buy_list)))
        LOGGER.debug(buy_list)

        for stocks in buy_list:
          trade_price = int(KOAPY_TRADER.GetStockBasicInfoAsDict(stocks['symbol_code'])['현재가'])
          quantity = fnGetQuantity(trade_price, KIWOOM_OPTION['money_per_buy'])
          LOGGER.debug('%s(%s): %s * %s = %s' % (stocks['name'], stocks['symbol_code'], fnCommify(trade_price), fnCommify(quantity), fnCommify(trade_price * quantity)))

          order_info = {
            'order': {
              'req_name': 'STOCKER_BUY_ORDER',
              'account_no': KIWOOM_OPTION['account_number'],
              'order_type': 1,
              'symbol_code': stocks['symbol_code'][1:] if stocks['symbol_code'][0] == 'A' else stocks['symbol_code'],
              'quantity': quantity,
              'price': 0,
              'quote_type': '03',
              'original_order_no': ''
            },
            'info': stocks
          }

          th = OrderWorker(stocks)
          th.start()

        # if STOCKER_OPTION['mode'] == 1:
        #   break

        if BUY_TRADE_LIST['start_idx'] >= len(BUY_TRADE_LIST['list']):
          LOGGER.info('Done. Buy Trading today')
          break
      else:
        LOGGER.info('KRX closed!')
        LOGGER.info('SELL WORKER TERMINATE')
        time.sleep(1)
        break
      time.sleep(1)

#=============================== Buy Sell Util Functions ===============================#
def fnCheckSellStocks(argHoldingStocks):
  global LOGGER
  global SELL_OPTION

  sell_list = {
    # INDEX: {
    #   INFO: {}
    #   REASON: []
    # }
  }

  LOGGER.debug('fnCheckSellStocks')

  # Notice Minimum Profit Cut
  LOGGER.info('Check Static Profit Cut: %.2f%%' % (SELL_OPTION['minimum_percentage'] * 100))

  # Check Static Profit Cut
  LOGGER.info('Check Static Profit Cut: %s (>=%.2f%%)' % (SELL_OPTION['static']['enabled'], SELL_OPTION['static']['percentage'] * 100))
  if SELL_OPTION['static']['enabled']:
    for idx, data in argHoldingStocks.iterrows():
      if data['수익률'] >= SELL_OPTION['static']['percentage']:
        LOGGER.info('%s is greater than static percentage(%.2f%%>=%.2f%%)' % (data['종목명'], data['수익률'] * 100, SELL_OPTION['static']['percentage'] * 100))
        if data['수익률'] < SELL_OPTION['minimum_percentage']:
          LOGGER.info('%s : Profit(%.2f%%) is lower than minimum_percentage(%.2f%%)' % (data['종목명'], data['수익률'] * 100, SELL_OPTION['minimum_percentage'] * 100))
        else:
          if idx not in sell_list:
            sell_list[idx] = {
            'info': argHoldingStocks.iloc[idx, :],
            'reason': []
          }
          sell_list[idx]['reason'].append('>=%.2f%%(Static)' % (SELL_OPTION['static']['percentage'] * 100))
  
  # Check Stats Profit Cut
  LOGGER.info('Check Stats Profit Cut: %s, %ddays (>=%.2f%%[KOSPI], >=%.2f%%[KOSDAQ])' % (SELL_OPTION['stats']['enabled'], SELL_OPTION['stats']['days'], SELL_OPTION['stats']['percentage']['KOSPI']['avg_profit_rate'] * 100, SELL_OPTION['stats']['percentage']['KOSDAQ']['avg_profit_rate'] * 100))
  if SELL_OPTION['stats']['enabled']:
    for idx, data in argHoldingStocks.iterrows():
      market = data['Market']
      if data['수익률'] >= SELL_OPTION['stats']['percentage'][market]['avg_profit_rate']:
        LOGGER.info('%s(%s) is greater than stats percentage(%.2f%%>=%.2f%%)' % (data['종목명'], market, data['수익률'] * 100, SELL_OPTION['stats']['percentage'][market]['avg_profit_rate'] * 100))
        if data['수익률'] < SELL_OPTION['minimum_percentage']:
          LOGGER.info('%s : Profit(%.2f%%) is lower than minimum_percentage(%.2f%%)' % (data['종목명'], data['수익률'] * 100, SELL_OPTION['minimum_percentage'] * 100))
        else:
          if idx not in sell_list:
            sell_list[idx] = {
            'info': argHoldingStocks.iloc[idx, :],
            'reason': []
          }
          sell_list[idx]['reason'].append('>=%.2f%%(Stats|%s)' % (SELL_OPTION['stats']['percentage'][market]['avg_profit_rate'] * 100, market))
  
  # Check Target Price
  LOGGER.info('Check Target Price Cut: %s' % (SELL_OPTION['target_price']['enabled']))
  if SELL_OPTION['target_price']['enabled']:
    for idx, data in argHoldingStocks.iterrows():
      if data['TargetPrice'] is not None and data['현재가'] >= data['TargetPrice']:
        LOGGER.info('%s is greater than target price(%s>=%s)' % (data['종목명'], fnCommify(data['현재가']), fnCommify(data['TargetPrice'])))
        if data['수익률'] < SELL_OPTION['minimum_percentage']:
          LOGGER.info('%s : Profit(%.2f%%) is lower than minimum_percentage(%.2f%%)' % (data['종목명'], data['수익률'] * 100, SELL_OPTION['minimum_percentage'] * 100))
        else:
          if idx not in sell_list:
            sell_list[idx] = {
            'info': argHoldingStocks.iloc[idx, :],
            'reason': []
          }
          sell_list[idx]['reason'].append('>=%d(TargetPrice)' % (data['TargetPrice']))

  # Check No More Buy

  # Check Speed Mode
  LOGGER.info('Check Speed Mode Profit Cut: %s (>=%.2f%%)' % (SELL_OPTION['speed_mode']['enabled'], SELL_OPTION['speed_mode']['percentage'] * 100))
  if SELL_OPTION['speed_mode']['enabled']:
    for idx, data in argHoldingStocks.iterrows():
      if data['수익률'] >= SELL_OPTION['speed_mode']['percentage']:
        LOGGER.info('%s(%s) is greater than speed mode percentage(%.2f%%>=%.2f%%)' % (data['종목명'], market, data['수익률'] * 100, SELL_OPTION['speed_mode']['percentage'] * 100))
        if data['수익률'] < SELL_OPTION['minimum_percentage']:
          LOGGER.info('%s : Profit(%.2f%%) is lower than minimum_percentage(%.2f%%)' % (data['종목명'], data['수익률'] * 100, SELL_OPTION['minimum_percentage'] * 100))
        else:
          if idx not in sell_list:
            sell_list[idx] = {
            'info': argHoldingStocks.iloc[idx, :],
            'reason': []
          }
          sell_list[idx]['reason'].append('>=%.2f%%(Speed Mode)' % (SELL_OPTION['speed_mode']['percentage'] * 100))

  return sell_list

def fnCheckBuyStocks():
  global LOGGER
  global KIWOOM_OPTION
  global BUY_OPTION

  buy_list = []

  LOGGER.debug('fnCheckBuyStocks')

  deposit = fnGetDepositInfo(KIWOOM_OPTION['account_number'])

  deposit = int(deposit['100%종목주문가능금액'])
  LOGGER.debug('Deposit: %s' % (fnCommify(deposit)))

  LOGGER.debug('Money per buy: %s' % (fnCommify(KIWOOM_OPTION['money_per_buy'])))

  available_count = math.floor(deposit / KIWOOM_OPTION['money_per_buy'])
  LOGGER.debug('Available Count: %s' % (fnCommify(available_count)))

  LOGGER.debug('Start idx: %d' % (BUY_TRADE_LIST['start_idx']))

  for stock in BUY_TRADE_LIST['list'][BUY_TRADE_LIST['start_idx']:(BUY_TRADE_LIST['start_idx'] + available_count)]:
    buy_list.append(stock)
  
  BUY_TRADE_LIST['start_idx'] += len(buy_list)
  LOGGER.debug('Start idx: %d' % (BUY_TRADE_LIST['start_idx']))
  
  return buy_list

def fnSettingBuyTradeList(argSignalList, argHoldingStocks):
  global LOGGER
  global BUY_OPTION

  LOGGER.debug('fnSettingBuyTradeList')

  trade_list = []
  for level in BUY_OPTION['level']:
    # remove holding stocks
    for stock in argSignalList['buy'][level]:
      if len(argHoldingStocks[(argHoldingStocks['종목코드'] == stock['symbol_code'])].index) == 0:
        trade_list.append(stock)

  return trade_list

def fnGetQuantity(argTradePrice, argMaxMoney):
  global LOGGER

  resQuantity = 0

  argTradePrice = abs(argTradePrice)

  LOGGER.debug('Trade Price: %s' % (fnCommify(argTradePrice)))
  LOGGER.debug('Max Money: %s' % (fnCommify(argMaxMoney)))

  if argMaxMoney >= argTradePrice:
    resQuantity = math.floor(argMaxMoney / argTradePrice)

  LOGGER.debug('result: %s' % (fnCommify(resQuantity)))

  return resQuantity

#=============================== Kiwoom Functions ===============================#
def fnGetAccountInfo():
  global LOGGER
  global KOAPY_TRADER

  LOGGER.debug('fnGetAccountInfo')
  return KOAPY_TRADER.GetAccountList()

def fnGetDepositInfo(argAccount):
  global LOGGER
  global KOAPY_TRADER

  LOGGER.debug('fnGetDepositInfo(%s)' % (argAccount))
  return KOAPY_TRADER.GetDepositInfo(argAccount)

def fnGetHoldingStocks(argAccount):
  global LOGGER
  global KIWOOM_OPTION
  global KOAPY_TRADER

  LOGGER.debug('fnGetHoldingStocks(%s)' % (argAccount))
  
  summary, holding_stocks = KOAPY_TRADER.GetAccountEvaluationBalanceAsSeriesAndDataFrame(argAccount)
  holding_stocks = holding_stocks.rename(columns={'종목번호': '종목코드', '수익률(%)': '수익률'})
  holding_stocks['수익률'] = pd.to_numeric(holding_stocks['수익률']) / 100
  # str to num
  holding_stocks['평가손익'] = pd.to_numeric(holding_stocks['평가손익'])
  holding_stocks['매입가'] = pd.to_numeric(holding_stocks['매입가'])
  holding_stocks['전일종가'] = pd.to_numeric(holding_stocks['전일종가'])
  holding_stocks['보유수량'] = pd.to_numeric(holding_stocks['보유수량'])
  holding_stocks['매매가능수량'] = pd.to_numeric(holding_stocks['매매가능수량'])
  holding_stocks['현재가'] = pd.to_numeric(holding_stocks['현재가'])
  holding_stocks['전일매수수량'] = pd.to_numeric(holding_stocks['전일매수수량'])
  holding_stocks['전일매도수량'] = pd.to_numeric(holding_stocks['전일매도수량'])
  holding_stocks['금일매수수량'] = pd.to_numeric(holding_stocks['금일매수수량'])
  holding_stocks['금일매도수량'] = pd.to_numeric(holding_stocks['금일매도수량'])
  holding_stocks['매입금액'] = pd.to_numeric(holding_stocks['매입금액'])
  holding_stocks['매입수수료'] = pd.to_numeric(holding_stocks['매입수수료'])
  holding_stocks['평가금액'] = pd.to_numeric(holding_stocks['평가금액'])
  holding_stocks['평가수수료'] = pd.to_numeric(holding_stocks['평가수수료'])
  holding_stocks['세금'] = pd.to_numeric(holding_stocks['세금'])
  holding_stocks['수수료합'] = pd.to_numeric(holding_stocks['수수료합'])
  holding_stocks['보유비중(%)'] = pd.to_numeric(holding_stocks['보유비중(%)'])
  holding_stocks['신용구분'] = pd.to_numeric(holding_stocks['신용구분'])

  holding_stocks = holding_stocks.drop(holding_stocks[(pd.to_numeric(holding_stocks['현재가']) == 0) & (pd.to_numeric(holding_stocks['평가금액']) == 0) & (pd.to_numeric(holding_stocks['수익률']) == 0)].index)
  
  return holding_stocks

#=============================== Main Functions ===============================#
def fnMain(argOptions, argArgs):
  global LOGGER
  global KIWOOM_OPTION
  global KOAPY_TRADER
  global WORKER
  global SIGNAL_LIST
  global BUY_TRADE_LIST

  try:
    fnLoadingOptions()

    if fnCheckOptions() is False:
      return False

    SIGNAL_LIST = fnGetConsensusInfo()

    KOAPY_TRADER = KiwoomOpenApiPlusEntrypoint()
    KOAPY_TRADER.EnsureConnected()
  
    account_list = fnGetAccountInfo()

    if KIWOOM_OPTION['account_number'] in account_list:
      LOGGER.info('%s in Account list' % (KIWOOM_OPTION['account_number']))
    else:
      LOGGER.info('%s is not found!' % (KIWOOM_OPTION['account_number']))
      return False

    BUY_TRADE_LIST['list'] = fnSettingBuyTradeList(SIGNAL_LIST, fnGetHoldingStocks(KIWOOM_OPTION['account_number']))

    # SELL MY STOCK
    WORKER['sell'] = SellWorker()
    WORKER['sell'].start()
    # END OF SELL
    
    # BUY MY STOCK
    WORKER['buy'] = BuyWorker()
    WORKER['buy'].start()
    # END OF BUY

    while True:
      if WORKER['sell'].isRunning():
        LOGGER.debug('Sell worker runnig')
      else:
        LOGGER.info('Sell work terminate')

      if WORKER['buy'].isRunning():
        LOGGER.debug('Buy worker runnig')
      else:
        LOGGER.info('Buy work terminate')

      if WORKER['sell'].isRunning() is False and WORKER['buy'].isRunning() is False:
        LOGGER.info('All terminated!')
        time.sleep(3)
        break
      
      time.sleep(5)
    return True
  except:
    LOGGER.error(' *** Error in Main.')
    LOGGER.debug(traceback.format_exc())
  finally:
    return True

#=============================== Request Functions ===============================#
def fnGetData(argURL, params=None, headers=None, argTryCount=5):
  global LOGGER

  # try:
  #   import http.client as http_client
  # except ImportError:
  #     # Python 2
  #     import httplib as http_client
  # http_client.HTTPConnection.debuglevel = 1

  # # You must initialize logging, otherwise you'll not see debug output.
  # logging.basicConfig()
  # logging.getLogger().setLevel(logging.DEBUG)
  # requests_log = logging.getLogger("requests.packages.urllib3")
  # requests_log.setLevel(logging.DEBUG)
  # requests_log.propagate = True

  res = None

  for try_count in range(argTryCount):
    try:
      res = requests.get(argURL, params=params, headers=headers)

      if(res.status_code == 200):
        break
    except:
      LOGGER.error('\t -x- Requests error:fnGetData() (Try: %02d / %02d)' % ((try_count + 1), argTryCount))
      time.sleep(1)

  if((try_count == argTryCount) or ((res != None) and (res.status_code != 200))):
    LOGGER.error('\t * data collecting error! (URL: %s, code: %s)' % (argURL, res.status_code))
    return None
  
  return res

#=============================== Consensus Functions ===============================#
def fnGetConsensusInfo():
  global LOGGER
  global STOCKER_URL
  global BUY_OPTION
  global CONNECTION_OPTION

  LOGGER.info('Get Consensus from web!')

  data = {
    "buy": [[], [], []],
    "sell": []
  }

  try:
    url = STOCKER_URL
    url += '/test/%s/%s'

    today = datetime.today().strftime("%Y-%m-%d")

    for target in data.keys():
      LOGGER.debug(target)
      for try_count in range(CONNECTION_OPTION['try_count']):
        try:
          if target == 'buy':
            for (idx, option) in enumerate(BUY_OPTION['level_option']):
              params = option
              params['level'] = idx

              LOGGER.debug('%s' % (url % (target, today)))

              res = fnGetData((url % (target, today)), params=params)

              data[target][idx] += res.json()['data']['rows']
          else:
            LOGGER.debug('%s' % (url % (target, today)))
            res = fnGetData((url % (target, today)))
            data[target] += res.json()['data']['rows']
          break
        except:
          LOGGER.error(res.text)
          LOGGER.error(traceback.format_exc())
          LOGGER.error(' -x- retry (%d / %d)' % (try_count + 1, CONNECTION_OPTION['try_count']))
  except:
    LOGGER.error(res.text)
    LOGGER.error(traceback.format_exc())
  finally:
    print(data)
    return data

def fnGetConsensusLatestInfo():
  global LOGGER
  global STOCKER_URL
  global CONNECTION_OPTION

  LOGGER.info('Get Consensus latest info from web!')

  data = None

  try:
    url = '%s/lastday' % (STOCKER_URL)

    for try_count in range(CONNECTION_OPTION['try_count']):
      try:
        res = fnGetData(url)
        data = res.json()['data']
        break
      except:
        LOGGER.error(res.text)
        LOGGER.error(traceback.format_exc())
        LOGGER.error(' -x- retry (%d / %d)' % (try_count + 1, CONNECTION_OPTION['try_count']))
  except:
    LOGGER.error(res.text)
    LOGGER.error(traceback.format_exc())
  finally:
    return data

def fnGetProfitCutStats():
  global LOGGER
  global STOCKER_URL
  global CONNECTION_OPTION
  global SELL_OPTION

  LOGGER.info('Get Profit cut info from web!')

  data = None

  try:
    url = '%s/stats?days=%d' % (STOCKER_URL, SELL_OPTION['stats']['days'])
    LOGGER.debug(url)

    for try_count in range(CONNECTION_OPTION['try_count']):
      try:
        res = fnGetData(url)
        data = res.json()['data']
        break
      except:
        LOGGER.error(res.text)
        LOGGER.error(traceback.format_exc())
        LOGGER.error(' -x- retry (%d / %d)' % (try_count + 1, CONNECTION_OPTION['try_count']))
  except:
    LOGGER.error(res.text)
    LOGGER.error(traceback.format_exc())
  finally:
    return data

def fnGetMoreInfoMyStock(argSymbolCodes):
  global LOGGER
  global STOCKER_URL
  global CONNECTION_OPTION

  LOGGER.info('Get More info from web!')

  data = None

  try:
    # url = '%s/info?date=%s&symbol_code=%s' % (STOCKER_URL, datetime.today().strftime("%Y-%m-%d"), ','.join(list(map(lambda x: 'A' + x, argSymbolCodes))))
    url = '%s/info?date=%s&symbol_code=%s' % (STOCKER_URL, datetime.today().strftime("%Y-%m-%d"), ','.join(argSymbolCodes))

    for try_count in range(CONNECTION_OPTION['try_count']):
      try:
        res = fnGetData(url)
        data = res.json()['data']
        break
      except:
        LOGGER.error(res.text)
        LOGGER.error(traceback.format_exc())
        LOGGER.error(' -x- retry (%d / %d)' % (try_count + 1, CONNECTION_OPTION['try_count']))
  except:
    LOGGER.error(res.text)
    LOGGER.error(traceback.format_exc())
  finally:
    return data

#=============================== Util Functions ===============================#
def fnCommify(argValue, argPoint=2):
  if type(argValue) is int:
    return format(argValue, ',')
  if type(argValue) is float:
    return format(argValue, (',.%df' % argPoint))

def fnCheckOpenKRX():
  now = Timestamp.now(tz=KRX_CALENDAR.tz)
  previous_open = KRX_CALENDAR.previous_open(now).astimezone(KRX_CALENDAR.tz)
  # https://github.com/quantopian/trading_calendars#why-are-open-times-one-minute-late
  if previous_open.minute % 5 == 1:
    previous_open -= timedelta(minutes=1)
  next_close = KRX_CALENDAR.next_close(previous_open).astimezone(KRX_CALENDAR.tz)
  return previous_open <= now <= next_close

#=============================== Loading & Check Option Function ===============================#
def fnLoadingOptions():
  global LOGGER
  global CONFIG
  global STOCKER_OPTION
  global SYSTEM_OPTION
  global CONNECTION_OPTION
  global KIWOOM_OPTION
  global BUY_OPTION
  global SELL_OPTION
  global TELEGRAM_OPTION

  try:
    # Loading Stocker Option
    if 'stcoker_option' in CONFIG:
      STOCKER_OPTION.update(CONFIG['stcoker_option'])

    # Loading System Option
    if 'system_option' in CONFIG:
      SYSTEM_OPTION.update(CONFIG['system_option'])
    
    # Loading Connection Option
    if 'connection_option' in CONFIG:
      CONNECTION_OPTION.update(CONFIG['connection_option'])
    
    # Loading Kiwoom Option
    if 'kiwoom_option' in CONFIG:
      KIWOOM_OPTION.update(CONFIG['kiwoom_option'])
    
    # Loading Buy Option
    if 'buy_option' in CONFIG:
      BUY_OPTION.update(CONFIG['buy_option'])
    
    # Loading Sell Option
    if 'sell_option' in CONFIG:
      SELL_OPTION.update(CONFIG['sell_option'])
    SELL_OPTION['stats']['percentage'] = fnGetProfitCutStats()
    SELL_OPTION['stats']['percentage']['KOSPI']['avg_profit_rate'] = round(SELL_OPTION['stats']['percentage']['KOSPI']['avg_profit_rate'], 2) / 100
    SELL_OPTION['stats']['percentage']['KOSDAQ']['avg_profit_rate'] = round(SELL_OPTION['stats']['percentage']['KOSDAQ']['avg_profit_rate'], 2) / 100
    # SELL_OPTION['static']['percentage'] *= 100
    # SELL_OPTION['stats']['percentage']['KOSPI']['avg_profit_rate'] *= 100
    # SELL_OPTION['stats']['percentage']['KOSDAQ']['avg_profit_rate'] *= 100
    # SELL_OPTION['no_more_buy']['percentage'] *= 100
    # SELL_OPTION['speed_mode']['percentage'] *= 100
    # SELL_OPTION['minimum_percentage'] *= 100
    
    # Loading Telegram Option
    if 'telegram_option' in CONFIG:
      TELEGRAM_OPTION.update(CONFIG['telegram_option'])

    LOGGER.debug(SYSTEM_OPTION)
    LOGGER.debug(CONNECTION_OPTION)
    LOGGER.debug(TELEGRAM_OPTION)
    LOGGER.debug(KIWOOM_OPTION)
    LOGGER.debug(BUY_OPTION)
    LOGGER.debug(SELL_OPTION)

    return True
  except:
    LOGGER.debug(traceback.format_exc())
  
  return False

def fnCheckOptions():
  global LOGGER
  global SYSTEM_OPTION
  global KIWOOM_OPTION
  global CONNECTION_OPTION
  global BUY_OPTION
  global SELL_OPTION
  global TELEGRAM_OPTION

  res_check = True

  try:
    # Check System Option
    LOGGER.info('System Option:')
    if 'auto_shutdown' in SYSTEM_OPTION:
      LOGGER.info('\tAuto Shutdown: %s' % (SYSTEM_OPTION['auto_shutdown']))
    
    # Check Connection Option
    LOGGER.info('Connection Option:')
    LOGGER.info('\tWaiting: %ds' % (CONNECTION_OPTION['waiting']))
    LOGGER.info('\tTry Count: %d' % (CONNECTION_OPTION['try_count']))

    # Check Telegram Option
    LOGGER.info('Telegram Option:')
    if 'enabled' in TELEGRAM_OPTION and TELEGRAM_OPTION['enabled']:
      if 'token' not in TELEGRAM_OPTION:
        LOGGER.info('\tTELEGRAM TOKEN IS NOT SETTING!')
        res_check = False
      else:
        LOGGER.info('\tToken: %s' % (TELEGRAM_OPTION['token']))

      if 'chat_id' not in TELEGRAM_OPTION:
        LOGGER.info('\tTELEGRAM CHAT ID IS NOT SETTING!')
        res_check = False
      else:
        LOGGER.info('\tChat ID: %s' % (TELEGRAM_OPTION['chat_id']))
    
    # Check Kiwoom Option
    LOGGER.info('Kiwoom Option:')
    if 'account_number' not in KIWOOM_OPTION:
      LOGGER.info('\tACCOUNT NUMBER IS NOT SETTING!')
      res_check = False
    else:
      LOGGER.info('\tAccount Number: %s' % (KIWOOM_OPTION['account_number']))
    
    if 'money_per_buy' not in KIWOOM_OPTION:
      LOGGER.info('\tMONEY PER BUY IS NOT SETTING!')
      res_check = False
    else:
      LOGGER.info('\tMoney per Buy: %s' % (fnCommify(KIWOOM_OPTION['money_per_buy'])))
    
    # Check Buy Option
    LOGGER.info('Buy Option:')
    LOGGER.info('\tBuy Level: %s' % (' -> '.join(list(map(lambda x: str(x), BUY_OPTION['level'])))))
    if len(BUY_OPTION['level']) == 0:
      LOGGER.info('\tBUY LEVEL OPTION IS EMPTY! NOT BUY!')

    if 'level_option' not in BUY_OPTION:
      LOGGER.info('\tBUY LEVEL OPTION IS NOT SETTING!')
      res_check = False
    elif len(BUY_OPTION['level_option']) == 0:
      LOGGER.info('\tBUY LEVEL OPTION IS EMPTY! NOT BUY!')
      BUY_OPTION['level'] = []
    else:
      LOGGER.info('\tBUY LEVEL OPTION:')
      for (idx, option) in enumerate(BUY_OPTION['level_option']):
        LOGGER.info('\t\t- Level %d: %s' % (idx, option))
    
    # Check Sell Option
    LOGGER.info('Sell Option:')
    # Profit Cut
    LOGGER.info('\tProfit Cut: %s' % (SELL_OPTION['static']['enabled']))
    if SELL_OPTION['static']['enabled'] is True and 'percentage' not in SELL_OPTION['static']:
      LOGGER.info('\tPROFIT CUT IS SET, BUT PERCENTAGE IS NOT SETTING!')
      res_check = False
    elif SELL_OPTION['static']['enabled'] is True and 'percentage' in SELL_OPTION['static']:
      LOGGER.info('\tProfit Cut Percentage: %.2f%%' % (SELL_OPTION['static']['percentage'] * 100))
    
    # Profit Cut by Stat
    LOGGER.info('\tProfit Cut by Stats: %s' % (SELL_OPTION['stats']['enabled']))
    if SELL_OPTION['stats']['enabled'] is True and 'days' not in SELL_OPTION['stats']:
      LOGGER.info('\tPROFIT CUT BY STATS IS SET, BUT DAYS IS NOT SETTING!')
      res_check = False
    elif SELL_OPTION['stats']['enabled'] is True and 'days' in SELL_OPTION['stats']:
      LOGGER.info('\tProfit Cut by Stats Days: %d' % (SELL_OPTION['stats']['days']))
      if 'percentage' in SELL_OPTION['stats']:
        LOGGER.info('\t\tKOSPI: %.2f%%' % (SELL_OPTION['stats']['percentage']['KOSPI']['avg_profit_rate'] * 100))
        LOGGER.info('\t\tKOSDAQ: %.2f%%' % (SELL_OPTION['stats']['percentage']['KOSDAQ']['avg_profit_rate'] * 100))
    
    # Target Price Cut
    LOGGER.info('\tTarget Price Cut: %s' % (SELL_OPTION['target_price']['enabled']))

    # No More Buy Profit Cut
    LOGGER.info('\tNo More Buy Profit Cut: %s' % (SELL_OPTION['no_more_buy']['enabled']))
    if SELL_OPTION['no_more_buy']['enabled'] is True and 'percentage' not in SELL_OPTION['no_more_buy']:
      LOGGER.info('\tNO MORE BUY PROFIT CUT IS SET, BUT PERCENTAGE IS NOT SETTING!')
      res_check = False
    elif SELL_OPTION['no_more_buy']['enabled'] is True and 'percentage' in SELL_OPTION['no_more_buy']:
      LOGGER.info('\tNo More Buy Profit Cut Percentage: %.2f%%' % (SELL_OPTION['no_more_buy']['percentage'] * 100))
    
    # Minimum Profit Cut
    LOGGER.info('\tMinimum Profit Cut Percentage: %.2f%%' % (SELL_OPTION['minimum_percentage'] * 100))
    if 'minimum_percentage' not in SELL_OPTION:
      LOGGER.info('\tMINIMUM PROFIT CUT PERCENTAGE IS NOT SETTING!')
      res_check = False

    LOGGER.debug(SYSTEM_OPTION)
    LOGGER.debug(CONNECTION_OPTION)
    LOGGER.debug(TELEGRAM_OPTION)
    LOGGER.debug(KIWOOM_OPTION)
    LOGGER.debug(BUY_OPTION)
    LOGGER.debug(SELL_OPTION)

    return True
  except:
    LOGGER.error(SYSTEM_OPTION)
    LOGGER.error(CONNECTION_OPTION)
    LOGGER.error(TELEGRAM_OPTION)
    LOGGER.error(KIWOOM_OPTION)
    LOGGER.error(BUY_OPTION)
    LOGGER.error(SELL_OPTION)
    LOGGER.error(traceback.format_exc())
  
  return res_check

#=============================== Config & Init Function ===============================#
def fnGetConfig(argConfigFilePath):
  global LOGGER
  global CONFIG

  CONFIG = fnReadJsonFile(argConfigFilePath)
  
  if len(CONFIG) != 0:
    return True
  
  return False

def fnReadJsonFile(argJsonFilePath):
  global LOGGER

  res = {}

  try:
    if os.path.isfile(argJsonFilePath):
      res = json.loads(open(argJsonFilePath, encoding='UTF8').read())
      LOGGER.info(' * Read json data')
    else:
      LOGGER.error(' * json file not found.')
  except:
    LOGGER.error(' *** Error read json file.')
    LOGGER.debug(traceback.format_exc())
  finally:
    return res

def fnInit(argOptions):
  global PROG_NAME
  global LOGGER
  global LOG_DIR
  global LOG_FILENAME

  if os.path.isdir(os.path.abspath(LOG_DIR)) is False:
    os.mkdir(os.path.abspath(LOG_DIR))

  LOGGER = logging.getLogger(PROG_NAME.replace(' ', ''))

  if argOptions.o_bVerbose is True:
    LOGGER.setLevel(logging.DEBUG)
  else:
    LOGGER.setLevel(logging.INFO)

  formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s - %(filename)s:%(lineno)s')
  
  file_handler = logging.handlers.TimedRotatingFileHandler(LOG_FILENAME, when='midnight', backupCount=7, encoding='UTF-8')
  file_handler.suffix = '%Y%m%d'
  file_handler.setFormatter(formatter)

  stream_handler = logging.StreamHandler()
  stream_handler.setFormatter(formatter)

  LOGGER.addHandler(file_handler)
  LOGGER.addHandler(stream_handler)

  if argOptions.o_sConfigFilePath != None:
    LOGGER.info('Config file("%s")' % (parsed_options.o_sConfigFilePath))
    fnGetConfig(parsed_options.o_sConfigFilePath)

  return True

#=============================== OptionParser Functions ===============================#
def fnSetOptions():
  global PROG_VER

  parser = None

  # Ref. https://docs.python.org/2/library/optparse.html#optparse-reference-guide
  options = [
    { 'Param': ('-c', '--config'), 'action': 'store', 'type': 'string', 'dest': 'o_sConfigFilePath', 'default': 'conf/config.conf', 'metavar': '<Config file path>', 'help': 'Set config file path.\t\tdefault) config.conf (contents type is JSON)' },
    { 'Param': ('-v', '--verbose'), 'action': 'store_true', 'dest': 'o_bVerbose', 'default': False, 'metavar': '<Verbose Mode>', 'help': 'Set verbose mode.\t\tdefault) False' }
  ]
  usage = '%prog [options] <File or Dir path>\n\tex) %prog test\\'

  parser = OptionParser(usage = usage, version = '%prog ' + PROG_VER)

  for option in options:
    param = option['Param']
    del option['Param']
    parser.add_option(*param, **option)

  return parser

def fnGetOptions(argParser):
  # NECESSARY OPTIONS
  # if len(sys.argv) == 1:
  #   return argParser.parse_args(['--help'])

  # # NECESSARY ARGV
  # if len(argParser.parse_args()[1]) == 0:
  #   return argParser.parse_args(['--help'])

  return argParser.parse_args()

if __name__ == '__main__':
  (parsed_options, argvs) = fnGetOptions(fnSetOptions())
  if fnInit(parsed_options):
    LOGGER.info('Start %s...' % (PROG_NAME))
    fnMain(parsed_options, argvs)
    LOGGER.info('Terminate %s...' % (PROG_NAME))