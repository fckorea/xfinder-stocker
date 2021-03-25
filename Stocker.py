#-*- coding: utf-8 -*-
#-------------------------------------------------------------------------------
# Name:        Stocker v2
# Purpose:
# Python version: 3.7.3
#
# Author:    fckorea
#
# Created:    2019-07-27
# (c) fckorea 2019
#-------------------------------------------------------------------------------

import os
import sys
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

from pandas import Timestamp
from trading_calendars import get_calendar

from SysTrader import SysTrader

PROG_NAME = 'Stocker'
PROG_VER = '1.0'
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
  'try_count': 3,
  'wait_interval': 1 
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
  'minimum': {
    'auto': False,
    'percentage': 0.1
  }
}
TELEGRAM_OPTION = {
  'enabled': False
}

ACCOUNT_INFO = {
  'account_number': None,
  'deposit': 0,
  'holding_stocks': []
}

APP = None
TRADER = None

# 오늘 온 시그널 종목
TODAY_SIGNAL = {
  'buy': [],
  'sell': []
}

# 오늘 주문할 종목
TODAY_ORDER_LIST = {
  'idx': {
    'buy': 0,
    'sell': 0
  },
  'buy': [],
  'sell': []
}

# 오늘 거래한 종목
TODAY_TRADING_LIST = {
  'buy': [],
  'sell': []
}

WORKER = {
  'buy': None,
  'sell': None
}

KRX_CALENDAR = get_calendar('XKRX')

#=============================== Worker Class ===============================#
class TerminateWorker(QThread):
  def run(self):
    global LOGGER
    global TODAY_TRADING_LIST
    global WORKER
    global APP

    while(True):
      if (WORKER['sell'] is not None and WORKER['sell'].isRunning() is False) and (WORKER['buy'] is not None and WORKER['buy'].isRunning() is False):
        LOGGER.debug('SELL, BUY WORKER is terminated!')
        if len(list(filter(lambda x: x['order_info']['status'] == 0, TODAY_TRADING_LIST['buy']))) == 0 and len(list(filter(lambda x: x['order_info']['status'] == 0, TODAY_TRADING_LIST['sell']))) == 0:
          LOGGER.debug('ORDER COMPLETE!')
          APP.quit()
        else:
          LOGGER.debug('BUT, THE ORDER HAS NOT BEEN COMPLETE!')
      time.sleep(5)

def orderCompleteCallback(argData):
  global LOGGER

  LOGGER.debug('ChejanDataCallback')

  # 종목코드에서 'A' 제거
  symbol_code = argData["종목코드"]
  if 'A' <= symbol_code[0] <= 'Z' or 'a' <= symbol_code[0] <= 'z':
    symbol_code = symbol_code[1:]

  if int(argData['미체결수량']) == 0:
    target = 'buy'

    if '매도' in argData['주문구분']:
      target = 'sell'
      LOGGER.debug('매도 완료!')
    elif '매수' in argData['주문구분']:
      LOGGER.debug('매수 완료!')

    if len(list(filter(lambda x: x['order_info']['symbol_code'] == symbol_code, TODAY_TRADING_LIST[target]))) == 1:
      idx = list(map(lambda x: x['order_info']['symbol_code'], TODAY_TRADING_LIST[target])).index(symbol_code)
      if idx != -1:
        TODAY_TRADING_LIST[target][idx]['order_info']['status'] = 1
        TODAY_TRADING_LIST[target][idx]['order_result'] = argData
        LOGGER.debug(TODAY_TRADING_LIST[target][idx])
    else:
      LOGGER.debug('It\'s my order!')
      LOGGER.debug(argData)

class SellWorker(QThread):
  def run(self):
    global LOGGER
    global STOCKER_OPTION
    global CONNECTION_OPTION
    global KIWOOM_OPTION
    global ACCOUNT_INFO
    global TODAY_ORDER_LIST
    global TODAY_TRADING_LIST
    global TRADER
    global WORKER

    while True:
      LOGGER.info('<<<<< CHECK SELL >>>>>')

      LOGGER.debug(ACCOUNT_INFO)
      LOGGER.debug(KIWOOM_OPTION)
      LOGGER.debug(TODAY_ORDER_LIST)

      ACCOUNT_INFO = fnUpdateAccountInfo(TRADER, ACCOUNT_INFO['account_number'])

      sell_list = fnCheckSellStocks(ACCOUNT_INFO['holding_stocks'])
      order_sell_list = []

      for sell_stock in sell_list:
        LOGGER.debug(sell_stock)
        # 종목코드에서 'A' 제거
        symbol_code = sell_stock['info']['종목코드']
        if 'A' <= symbol_code[0] <= 'Z' or 'a' <= symbol_code[0] <= 'z':
          symbol_code = symbol_code[1:]
        
        sell_info = {
          'info': sell_stock['info'],
          'reason': sell_stock['reason'],
          'order_info': {
            'status': 0,
            'symbol_code': symbol_code,
            'name': sell_stock['info']['종목명'],
            'trade_price': abs(sell_stock['info']['현재가']),
            'quantity': sell_stock['info']['보유수량']
          }
        }
        sell_info['order_info']['predict_sum'] = sell_info['order_info']['trade_price'] * sell_info['order_info']['quantity']
        LOGGER.debug(sell_info)
        order_sell_list.append(sell_info)
      
      for order_sell in order_sell_list:
        LOGGER.info('Send Order (%s, %s, %d, %d)' % (order_sell['order_info']['symbol_code'], order_sell['order_info']['name'], order_sell['order_info']['trade_price'], order_sell['order_info']['quantity']))
        TRADER.kiwoom_SendOrder('STOCKER_SELL_ORDER', '2222', ACCOUNT_INFO['account_number'], 2, order_sell['order_info']['symbol_code'], order_sell['order_info']['quantity'], 0, '03', '')
        TODAY_TRADING_LIST['sell'].append(order_sell)

      time.sleep(STOCKER_OPTION['realtime_interval'])

      if STOCKER_OPTION['mode'] == 1:
        LOGGER.info('STOCKER MODE IS 1, WAIT SELL ORDER COMPLETE!')
        while True:
          if len(list(filter(lambda x: x['order_info']['status'] == 0, TODAY_TRADING_LIST['sell']))) == 0:
            LOGGER.info('SELL ORDER COMPLETE!')
            break
          time.sleep(1)
        break

    time.sleep(3)
    LOGGER.info('<<<<< END OF SELL WORKER >>>>>')

class BuyWorker(QThread):
  def run(self):
    global LOGGER
    global STOCKER_OPTION
    global CONNECTION_OPTION
    global KIWOOM_OPTION
    global ACCOUNT_INFO
    global TODAY_ORDER_LIST
    global TODAY_TRADING_LIST
    global TRADER
    global WORKER

    if STOCKER_OPTION['mode'] == 1:
      LOGGER.info('STOCKER MODE IS 1, Wait SELL WORKER!!')
      while True:
        if WORKER['sell'] is not None and WORKER['sell'].isRunning() is False:
          break
        time.sleep(1)

    while True:
      LOGGER.info('<<<<< CHECK BUY >>>>>')

      LOGGER.debug(ACCOUNT_INFO)
      LOGGER.debug(KIWOOM_OPTION)
      LOGGER.debug(TODAY_ORDER_LIST)

      ACCOUNT_INFO = fnUpdateAccountInfo(TRADER, ACCOUNT_INFO['account_number'])

      if ACCOUNT_INFO['deposit'] < KIWOOM_OPTION['money_per_buy']:
        LOGGER.info('주문 가능 금액이 설정된 종목당구매금액 보다 적어 구매를 할 수 없습니다.')
        LOGGER.info('주문가능금액: %s원' % (fnCommify(ACCOUNT_INFO['deposit'])))
        LOGGER.info('종목당구매금액: %s원' % (fnCommify(KIWOOM_OPTION['money_per_buy'])))
      else:
        buy_list = fnCheckBuyStocks()
        order_buy_list = []

        for buy_stock in buy_list:
          LOGGER.debug(buy_stock)
          stock_info = fnGetStockInfo(TRADER, buy_stock['symbol_code'])
          LOGGER.debug(stock_info)

          if stock_info['현재가'] == 0:
            LOGGER.error('Trade Price is 0! (%s, %s)' % (stock_info['종목코드'], stock_info['종목명']))
          else:
            buy_info = {
              'info': stock_info,
              'order_info': {
                'status': 0,
                'symbol_code': stock_info['종목코드'],
                'name': stock_info['종목명'],
                'trade_price': abs(stock_info['현재가']),
                'quantity': fnGetQuantity(stock_info['현재가'], KIWOOM_OPTION['money_per_buy'])
              }
            }
            buy_info['order_info']['predict_sum'] = buy_info['order_info']['trade_price'] * buy_info['order_info']['quantity']
            LOGGER.debug(buy_info)
            order_buy_list.append(buy_info)
        
        for order_buy in order_buy_list:
          LOGGER.info('Send Order (%s, %s, %d, %d)' % (order_buy['order_info']['symbol_code'], order_buy['order_info']['name'], order_buy['order_info']['trade_price'], order_buy['order_info']['quantity']))
          TRADER.kiwoom_SendOrder('STOCKER_BUY_ORDER', '1111', ACCOUNT_INFO['account_number'], 1, order_buy['order_info']['symbol_code'], order_buy['order_info']['quantity'], 0, '03', '')
          TODAY_TRADING_LIST['buy'].append(order_buy)

      time.sleep(STOCKER_OPTION['realtime_interval'])

      if STOCKER_OPTION['mode'] == 1:
        LOGGER.info('STOCKER MODE IS 1, WAIT BUY ORDER COMPLETE!')
        while True:
          if len(list(filter(lambda x: x['order_info']['status'] == 0, TODAY_TRADING_LIST['buy']))) == 0:
            LOGGER.info('BUY ORDER COMPLETE!')
            break
          time.sleep(1)
        break

    time.sleep(3)
    LOGGER.info('<<<<< END OF BUY WORKER >>>>>')

#=============================== Buy Sell Util Functions ===============================#
def fnCheckSellStocks(argHoldingStocks):
  global LOGGER
  global SELL_OPTION

  sell_list = {
    # SYMBOL_CODE: {
    #   INFO: {}
    #   REASON: []
    # }
  }

  LOGGER.debug('fnCheckSellStocks')

  # Notice Minimum Profit Cut
  LOGGER.info('Minimum Profit Cut Percentage: %.2f%%' % (SELL_OPTION['minimum']['percentage'] * 100))

  for stock in argHoldingStocks:
    LOGGER.debug('Check %s, %s, %.2f%%' % (stock['종목코드'], stock['종목명'], stock['수익률(%)']))

    # Check Static Profit Cut
    LOGGER.info('Check Static Profit Cut: %s (>=%.2f%%)' % (SELL_OPTION['static']['enabled'], SELL_OPTION['static']['percentage'] * 100))
    if SELL_OPTION['static']['enabled'] is True:
      profit_cut = (SELL_OPTION['static']['percentage'] * 100)
      reason = '>=%.2f%%(Static)' % (profit_cut)
      
      if SELL_OPTION['static']['percentage'] < SELL_OPTION['minimum']['percentage']:
        LOGGER.debug('re-check profit cut (%.2f%% => %.2f%%)' % (profit_cut, (SELL_OPTION['minimum']['percentage'] * 100)))
        profit_cut = (SELL_OPTION['minimum']['percentage'] * 100)
        reason = '>=%.2f%%(Static<minimum>)' % (profit_cut)

      if stock['수익률(%)'] >= profit_cut:
        LOGGER.info('%s is greater than static percentage(%.2f%%>=%.2f%%)' % (stock['종목명'], stock['수익률(%)'], profit_cut))
        if stock['종목코드'] not in sell_list:
          sell_list[stock['종목코드']] = {
            'info': stock,
            'reason': []
          }
        sell_list[stock['종목코드']]['reason'].append(reason)

    # Check Stats Profit Cut
    LOGGER.info('Check Stats Profit Cut: %s, %ddays (>=%.2f%%[KOSPI], >=%.2f%%[KOSDAQ])' % (SELL_OPTION['stats']['enabled'], SELL_OPTION['stats']['days'], SELL_OPTION['stats']['percentage']['KOSPI']['percentage'] * 100, SELL_OPTION['stats']['percentage']['KOSDAQ']['percentage'] * 100))
    if SELL_OPTION['stats']['enabled'] is True:
      market = stock['MORE_INFO']['market']
      profit_cut = SELL_OPTION['stats']['percentage'][market]['percentage'] * 100
      reason = '>=%.2f%%(Stats|%s)' % (profit_cut, market)
      
      if profit_cut < SELL_OPTION['minimum']['percentage']:
        LOGGER.debug('re-check profit cut (%.2f%% => %.2f%%)' % (profit_cut, (SELL_OPTION['minimum']['percentage'] * 100)))
        profit_cut = (SELL_OPTION['minimum']['percentage'] * 100)
        reason = '>=%.2f%%(Stats|%s)' % (profit_cut, market)

      if stock['수익률(%)'] >= profit_cut:
        LOGGER.info('%s(%s) is greater than stats percentage(%.2f%%>=%.2f%%)' % (stock['종목명'], market, stock['수익률(%)'], profit_cut))
        if stock['종목코드'] not in sell_list:
          sell_list[stock['종목코드']] = {
            'info': stock,
            'reason': []
          }
        sell_list[stock['종목코드']]['reason'].append(reason)

    # Check Target Price
    LOGGER.info('Check Target Price Cut: %s' % (SELL_OPTION['target_price']['enabled']))
    if SELL_OPTION['target_price']['enabled'] is True:
      reason = '>=%s(TargetPrice>=%.2f%%)' % (stock['MORE_INFO']['target_price'], (SELL_OPTION['minimum']['percentage'] * 100))

      if stock['MORE_INFO']['target_price'] is not None and stock['현재가'] >= stock['MORE_INFO']['target_price']:
        LOGGER.info('%s is greater than target price(%s>=%s)' % (stock['종목명'], fnCommify(stock['현재가']), fnCommify(stock['MORE_INFO']['target_price'])))

        if stock['수익률(%)'] >= (SELL_OPTION['minimum']['percentage'] * 100):
          if stock['종목코드'] not in sell_list:
            sell_list[stock['종목코드']] = {
              'info': stock,
              'reason': []
            }
          sell_list[stock['종목코드']]['reason'].append(reason)
        else:
          LOGGER.info('%s : Profit(%.2f%%) is lower than minimum percentage(%.2f%%)' % (stock['종목명'], stock['수익률(%)'], SELL_OPTION['minimum']['percentage'] * 100))

  # Check No More Buy

  # Check Speed Mode
  LOGGER.info('Check Speed Mode Profit Cut: %s (>=%.2f%%)' % (SELL_OPTION['speed_mode']['enabled'], SELL_OPTION['speed_mode']['percentage'] * 100))
  if SELL_OPTION['speed_mode']['enabled'] is True:
    profit_cut = (SELL_OPTION['speed_mode']['percentage'] * 100)
    reason = '>=%.2f%%(Speed Mode)' % (SELL_OPTION['speed_mode']['percentage'] * 100)

    if stock['수익률(%)'] >= profit_cut and len(list(filter(lambda x: x['order_info']['status'] == 0 and x['order_info'] == stock['종목코드']))) == 1:
      LOGGER.info('%s(%s) is greater than speed mode percentage(%.2f%%>=%.2f%%)' % (stock['종목명'], market, stock['수익률(%)'], SELL_OPTION['speed_mode']['percentage'] * 100))

      if stock['종목코드'] not in sell_list:
        sell_list[stock['종목코드']] = {
          'info': stock,
          'reason': []
        }
      sell_list[stock['종목코드']]['reason'].append(reason)

  return list(map(lambda x: sell_list[x], sell_list))

def fnCheckBuyStocks():
  global LOGGER
  global KIWOOM_OPTION
  global TODAY_ORDER_LIST
  global TRADER

  buy_list = []

  LOGGER.debug('fnCheckBuyStocks')

  deposit = fnGetDepositInfo(TRADER, KIWOOM_OPTION['account_number'])

  deposit = int(deposit['D+2추정예수금'])
  LOGGER.debug('Deposit: %s' % (fnCommify(deposit)))

  LOGGER.debug('Money per buy: %s' % (fnCommify(KIWOOM_OPTION['money_per_buy'])))

  available_count = math.floor(deposit / KIWOOM_OPTION['money_per_buy'])
  LOGGER.debug('Available Count: %s' % (fnCommify(available_count)))

  LOGGER.debug('Start idx: %d' % (TODAY_ORDER_LIST['idx']['buy']))

  if len(TODAY_ORDER_LIST['buy']) <= TODAY_ORDER_LIST['idx']['buy']:
    LOGGER.debug('No more buy stock! (len: %d, idx: %d)' % (len(TODAY_ORDER_LIST['buy']), TODAY_ORDER_LIST['idx']['buy']))
  else:
    for stock in TODAY_ORDER_LIST['buy'][TODAY_ORDER_LIST['idx']['buy']:(TODAY_ORDER_LIST['idx']['buy'] + available_count)]:
      buy_list.append(stock)

    TODAY_ORDER_LIST['idx']['buy'] += len(buy_list)
    LOGGER.debug('Setting Start idx: %d' % (TODAY_ORDER_LIST['idx']['buy']))
  
  return buy_list

def fnGetOrderList(argSignalList, argHoldingStocks):
  global LOGGER
  global BUY_OPTION

  LOGGER.debug('fnGetOrderList')

  order_list = {
    'buy': [],
    'sell': []
  }

  holding_stocks_code = list(map(lambda x: x['종목코드'], argHoldingStocks))

  # Set buy
  for level in BUY_OPTION['level']:
    # remove holding stocks
    for stock in argSignalList['buy'][level]:
      if stock['symbol_code'] not in holding_stocks_code and stock['symbol_code'] not in list(map(lambda x: x['symbol_code'], order_list['buy'])):
        if stock['symbol_code'][-1] == '0':
          order_list['buy'].append(stock)
        else:
          LOGGER.debug('Stock(%s, %s) is preferred stock!' % (stock['symbol_code']['name']))

  return order_list

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
def fnLogin():
  global LOGGER

  LOGGER.debug('fnLogin')

  trader = SysTrader.Kiwoom()

  # login
  if trader.kiwoom_GetConnectState() == 0:
    LOGGER.debug('로그인 시도')
    res = trader.kiwoom_CommConnect()
    LOGGER.debug('로그인 결과: {}'.format(res))

  return (trader, res)

def fnGetAccountInfo(argTrader):
  global LOGGER

  LOGGER.debug('fnGetAccountInfo')
  return argTrader.GetAccountList()

def fnGetDepositInfo(argTrader, argAccount):
  global LOGGER
  global CONNECTION_OPTION

  LOGGER.debug('fnGetDepositInfo(%s)' % (argAccount))

  argTrader.kiwoom_TR_OPW00004_계좌평가현황요청(argAccount)

  while True:
    if argTrader.result['updated'] is True:
      break
    time.sleep(CONNECTION_OPTION['wait_interval'])

  return argTrader.result['계좌평가현황요청']

def fnGetHoldingStocks(argTrader, argAccount):
  global LOGGER
  global CONNECTION_OPTION

  LOGGER.debug('fnGetHoldingStocks(%s)' % (argAccount))

  argTrader.kiwoom_TR_opw00018_계좌평가잔고내역요청(argAccount)

  while True:
    if argTrader.result['updated'] is True:
      break
    time.sleep(CONNECTION_OPTION['wait_interval'])
    
  holding_stocks = list(filter(lambda x: x['보유수량'] != 0, argTrader.result['계좌평가잔고내역요청']))

  more_info = fnGetMoreInfoMyStock(list(map(lambda x: x['종목코드'], holding_stocks)), )

  more_info_symbols = list(map(lambda x: x['symbol_code'], more_info))

  for (idx, stock) in enumerate(holding_stocks):
    if stock['종목코드'] in more_info_symbols:
      m_idx = more_info_symbols.index(stock['종목코드'])
      holding_stocks[idx]['MORE_INFO'] = more_info[m_idx]
      holding_stocks[idx]['market'] = more_info[m_idx]['market']
      holding_stocks[idx]['market_rank'] = more_info[m_idx]['market_rank']
      holding_stocks[idx]['level'] = more_info[m_idx]['lyr']
      holding_stocks[idx]['target_price'] = more_info[m_idx]['target_price']
  
  return holding_stocks

def fnUpdateAccountInfo(argTrader, argAccount):
  global LOGGER
  global ACCOUNT_INFO

  res = ACCOUNT_INFO

  res['deposit_info'] = fnGetDepositInfo(argTrader, argAccount)
  res['deposit'] = res['deposit_info']['D+2추정예수금']
  res['holding_stocks'] = fnGetHoldingStocks(argTrader, argAccount)

  return res

def fnGetStockInfo(argTrader, argSymbolCode):
  global LOGGER
  global CONNECTION_OPTION
  
  LOGGER.debug('fnGetStockInfo(%s)' % (argSymbolCode))

  for try_count in range(CONNECTION_OPTION['try_count']):
    try:
      argTrader.kiwoom_TR_OPT10001_주식기본정보요청(argSymbolCode)

      time.sleep(CONNECTION_OPTION['wait_interval'])

      stock_info = argTrader.result['주식기본정보']
      LOGGER.debug(stock_info)

      if abs(stock_info['시가']) == 0:
        LOGGER.debug('Trade price is 0')
        LOGGER.error(' -x- retry (%d / %d)' % (try_count + 1, CONNECTION_OPTION['try_count']))
        continue
      else:
        break
    except:
      LOGGER.error(traceback.format_exc())
      LOGGER.error(' -x- retry (%d / %d)' % (try_count + 1, CONNECTION_OPTION['try_count']))

  return stock_info

#=============================== Main Functions ===============================#
def fnMain(argOptions, argArgs):
  global LOGGER
  global CONNECTION_OPTION
  global BUY_OPTION
  global APP
  global TRADER
  global ACCOUNT_INFO
  global TODAY_SIGNAL
  global TODAY_ORDER_LIST
  global TODAY_TRADING_LIST

  try:
    fnLoadingOptions()

    if fnCheckOptions() is False:
      return False
    else:
      fnSettingOptions()
      if fnCheckOptions() is False:
        return False
      else:
        LOGGER.info('Option Check Complete!')

    APP = QApplication(sys.argv)

    terminateWorker = TerminateWorker()
    terminateWorker.start()

    for try_count in range(CONNECTION_OPTION['try_count']):
      try:
        (TRADER, login_status) = fnLogin()
        if login_status['data']['status'] == 0:
          LOGGER.info('Login Success!')
        else:
          LOGGER.info('Login failed...')
        break
      except:
        LOGGER.error(traceback.format_exc())
        LOGGER.error(' -x- retry (%d / %d)' % (try_count + 1, CONNECTION_OPTION['try_count']))

    # Set Real Opening (Not working? why??)
    # TRADER.kiwoom_SetRealReg('2000', '', '215;20;214', 0)
    # End of Set Real Opening

    account_list = TRADER.kiwoom_GetAccList()

    if KIWOOM_OPTION['account_number'] not in account_list:
      LOGGER.error('Account is not found(%s)' % (KIWOOM_OPTION['account_number']))
      APP.quit()
      return False
    
    ACCOUNT_INFO['account_number'] = KIWOOM_OPTION['account_number']

    # ACCOUNT_INFO['deposit_info'] = fnGetDepositInfo(ACCOUNT_INFO['account_number'])
    # ACCOUNT_INFO['deposit'] = ACCOUNT_INFO['deposit_info']['D+2추정예수금']
    # ACCOUNT_INFO['holding_stocks'] = fnGetHoldingStocks(ACCOUNT_INFO['account_number'])

    ACCOUNT_INFO = fnUpdateAccountInfo(TRADER, ACCOUNT_INFO['account_number'])

    TODAY_SIGNAL = fnGetConsensusInfo(BUY_OPTION)
    TODAY_ORDER_LIST.update(fnGetOrderList(TODAY_SIGNAL, ACCOUNT_INFO['holding_stocks']))

    TRADER.dict_callback['주문체결'] = orderCompleteCallback

    # fnSettingOrderList()

    # TEST
    LOGGER.debug(ACCOUNT_INFO)
    LOGGER.debug(TODAY_SIGNAL)
    LOGGER.debug(TODAY_ORDER_LIST)
    # End of TEST

    # EXECUTE SELL WORKER
    WORKER['sell'] = SellWorker()
    WORKER['sell'].start()
    # END OF SELL
    
    # EXECUTE BUY WORKER
    WORKER['buy'] = BuyWorker()
    WORKER['buy'].start()
    # END OF BUY

    APP.exec_()

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
def fnGetConsensusInfo(argBuyOption):
  global LOGGER
  global CONNECTION_OPTION
  global STOCKER_URL

  LOGGER.info('Get Consensus from web!')

  data = {
    "buy": [[], [], []],
    "sell": []
  }

  try:
    url = STOCKER_URL
    url += '/test/%s/%s'

    today = datetime.today().strftime("%Y-%m-%d")
    
    # TEST
    today = '2021-03-25'
    # End of TEST

    for target in data.keys():
      for try_count in range(CONNECTION_OPTION['try_count']):
        try:
          if target == 'buy':
            for (idx, option) in enumerate(argBuyOption['level_option']):
              params = option
              params['level'] = idx

              res = fnGetData((url % (target, today)), params=params)

              data[target][idx] += res.json()['data']['rows']
          else:
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
    return data

def fnGetConsensusLatestInfo():
  global LOGGER
  global CONNECTION_OPTION
  global STOCKER_URL

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

def fnGetProfitCutStats(argDays=60):
  global LOGGER
  global CONNECTION_OPTION
  global STOCKER_URL

  LOGGER.info('Get Profit cut info from web!')

  data = None

  try:
    url = '%s/stats?days=%d' % (STOCKER_URL, argDays)

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

def fnGetMoreInfoMyStock(argStocksCodes):
  global LOGGER
  global CONNECTION_OPTION
  global STOCKER_URL

  LOGGER.info('Get More info from web!')

  data = None

  try:
    url = '%s/info?date=%s&symbol_code=%s' % (STOCKER_URL, datetime.today().strftime("%Y-%m-%d"), ','.join(argStocksCodes))

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
      SELL_OPTION['stats']['percentage'] = fnGetProfitCutStats(SELL_OPTION['stats']['days'])
      SELL_OPTION['stats']['percentage']['KOSPI']['percentage'] = SELL_OPTION['stats']['percentage']['KOSPI']['avg_profit_rate'] / 100
      SELL_OPTION['stats']['percentage']['KOSDAQ']['percentage'] = SELL_OPTION['stats']['percentage']['KOSDAQ']['avg_profit_rate'] / 100
      
    # Loading Telegram Option
    if 'telegram_option' in CONFIG:
      TELEGRAM_OPTION.update(CONFIG['telegram_option'])

    LOGGER.debug(STOCKER_OPTION)
    LOGGER.debug(SYSTEM_OPTION)
    LOGGER.debug(CONNECTION_OPTION)
    LOGGER.debug(KIWOOM_OPTION)
    LOGGER.debug(BUY_OPTION)
    LOGGER.debug(SELL_OPTION)
    LOGGER.debug(TELEGRAM_OPTION)

    return True
  except:
    LOGGER.error(STOCKER_OPTION)
    LOGGER.error(SYSTEM_OPTION)
    LOGGER.error(CONNECTION_OPTION)
    LOGGER.error(KIWOOM_OPTION)
    LOGGER.error(BUY_OPTION)
    LOGGER.error(SELL_OPTION)
    LOGGER.error(TELEGRAM_OPTION)
    LOGGER.error(traceback.format_exc())
  
  return False

def fnCheckOptions():
  global LOGGER
  global STOCKER_OPTION
  global SYSTEM_OPTION
  global CONNECTION_OPTION
  global KIWOOM_OPTION
  global BUY_OPTION
  global SELL_OPTION
  global TELEGRAM_OPTION

  res_check = True

  try:
    # Check Stocker Option
    LOGGER.info('Stocker Option:')
    if 'mode' in STOCKER_OPTION:
      LOGGER.info('\tMode: %s' % (STOCKER_OPTION['mode']))
      LOGGER.info('\tRealtime Interval: %ds' % (STOCKER_OPTION['realtime_interval']))

    # Check System Option
    LOGGER.info('System Option:')
    if 'auto_shutdown' in SYSTEM_OPTION:
      LOGGER.info('\tAuto Shutdown: %s' % (SYSTEM_OPTION['auto_shutdown']))
    
    # Check Connection Option
    LOGGER.info('Connection Option:')
    LOGGER.info('\tWaiting: %ds' % (CONNECTION_OPTION['waiting']))
    LOGGER.info('\tTry Count: %d' % (CONNECTION_OPTION['try_count']))

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
      LOGGER.info('\t\tKOSPI: %.2f%%' % (SELL_OPTION['stats']['percentage']['KOSPI']['percentage'] * 100))
      LOGGER.info('\t\tKOSDAQ: %.2f%%' % (SELL_OPTION['stats']['percentage']['KOSDAQ']['percentage'] * 100))
    
    # Target Price Cut
    LOGGER.info('\tTarget Price Cut: %s' % (SELL_OPTION['target_price']['enabled']))

    # No More Buy Profit Cut
    LOGGER.info('\tNo More Buy Profit Cut: %s' % (SELL_OPTION['no_more_buy']['enabled']))
    if SELL_OPTION['no_more_buy']['enabled'] is True and 'percentage' not in SELL_OPTION['no_more_buy']:
      LOGGER.info('\tNO MORE BUY PROFIT CUT IS SET, BUT PERCENTAGE IS NOT SETTING!')
      res_check = False
    elif SELL_OPTION['no_more_buy']['enabled'] is True and 'percentage' in SELL_OPTION['no_more_buy']:
      LOGGER.info('\tNo More Buy Profit Cut Percentage: %.2f%%' % (SELL_OPTION['no_more_buy']['percentage'] * 100))
    
    # Speed Mode Profit Cut
    LOGGER.info('\tSpeed Mode: %s' % (SELL_OPTION['speed_mode']['enabled']))
    LOGGER.info('\tSpeed Mode Profit Cut Percentage: %.2f%%' % (SELL_OPTION['speed_mode']['percentage'] * 100))
    if 'speed_mode' not in SELL_OPTION:
      LOGGER.info('\tSPEED MODE PROFIT CUT PERCENTAGE IS NOT SETTING!')

    # Minimum Profit Cut
    LOGGER.info('\tMinimum Profit Cut Auto: %s' % (SELL_OPTION['minimum']['auto']))
    LOGGER.info('\tMinimum Profit Cut Percentage: %.2f%%' % (SELL_OPTION['minimum']['percentage'] * 100))
    if 'minimum' not in SELL_OPTION:
      LOGGER.info('\tMINIMUM PROFIT CUT PERCENTAGE IS NOT SETTING!')
      res_check = False
    
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

    LOGGER.debug(STOCKER_OPTION)
    LOGGER.debug(SYSTEM_OPTION)
    LOGGER.debug(CONNECTION_OPTION)
    LOGGER.debug(KIWOOM_OPTION)
    LOGGER.debug(BUY_OPTION)
    LOGGER.debug(SELL_OPTION)
    LOGGER.debug(TELEGRAM_OPTION)

    return True
  except:
    LOGGER.error(STOCKER_OPTION)
    LOGGER.error(SYSTEM_OPTION)
    LOGGER.error(CONNECTION_OPTION)
    LOGGER.error(KIWOOM_OPTION)
    LOGGER.error(BUY_OPTION)
    LOGGER.error(SELL_OPTION)
    LOGGER.error(TELEGRAM_OPTION)
    LOGGER.error(traceback.format_exc())
  
  return res_check

def fnSettingOptions():
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
    # Setting Sell Option
    ## Setting Minimum
    if SELL_OPTION['minimum']['auto'] is True:
      stats30 = fnGetProfitCutStats(30)
      trend = (stats30['KOSPI']['avg_profit_rate'] + stats30['KOSDAQ']['avg_profit_rate']) / 2

      LOGGER.debug('stats 30days KOSPI: %.2f%%, KOSDAQ: %.2f%%' % (stats30['KOSPI']['avg_profit_rate'], stats30['KOSDAQ']['avg_profit_rate']))
      LOGGER.debug('stats trend is %.2f%%' % (trend))

      if trend < 0:
        SELL_OPTION['minimum']['percentage'] = 0.03
      elif trend < 5:
        SELL_OPTION['minimum']['percentage'] = 0.05
      elif trend < 7:
        SELL_OPTION['minimum']['percentage'] = 0.07
      elif trend < 10:
        SELL_OPTION['minimum']['percentage'] = 0.1

      LOGGER.info('minimum > percentage re-setted! (%.2f%%)' % (SELL_OPTION['minimum']['percentage'] * 100))
    
    # ## Setting static
    # if SELL_OPTION['static']['enabled'] is True:
    #   if SELL_OPTION['static']['percentage'] < SELL_OPTION['minimum']['percentage']:
    #     SELL_OPTION['static']['percentage'] = SELL_OPTION['minimum']['percentage']
    #     LOGGER.info('static > percentage re-setted! (%.2f%%)' % (SELL_OPTION['static']['percentage'] * 100))
    
    # ## Setting stats
    # if SELL_OPTION['stats']['enabled'] is True:
    #   if SELL_OPTION['stats']['percentage']['KOSPI']['percentage'] < SELL_OPTION['minimum']['percentage']:
    #     SELL_OPTION['stats']['percentage']['KOSPI']['percentage'] = SELL_OPTION['minimum']['percentage']
    #     LOGGER.info('stats > percentage > KOSPI re-setted! (%.2f%%)' % (SELL_OPTION['stats']['percentage']['KOSPI']['percentage'] * 100))

    #   if SELL_OPTION['stats']['percentage']['KOSDAQ']['percentage'] < SELL_OPTION['minimum']['percentage']:
    #     SELL_OPTION['stats']['percentage']['KOSDAQ']['percentage'] = SELL_OPTION['minimum']['percentage']
    #     LOGGER.info('stats > percentage > KOSDAQ re-setted! (%.2f%%)' % (SELL_OPTION['stats']['percentage']['KOSDAQ']['percentage'] * 100))

    LOGGER.debug(STOCKER_OPTION)
    LOGGER.debug(SYSTEM_OPTION)
    LOGGER.debug(CONNECTION_OPTION)
    LOGGER.debug(KIWOOM_OPTION)
    LOGGER.debug(BUY_OPTION)
    LOGGER.debug(SELL_OPTION)
    LOGGER.debug(TELEGRAM_OPTION)

    return True
  except:
    LOGGER.error(STOCKER_OPTION)
    LOGGER.error(SYSTEM_OPTION)
    LOGGER.error(CONNECTION_OPTION)
    LOGGER.error(KIWOOM_OPTION)
    LOGGER.error(BUY_OPTION)
    LOGGER.error(SELL_OPTION)
    LOGGER.error(TELEGRAM_OPTION)
    LOGGER.error(traceback.format_exc())
  
  return False

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

  formatter = logging.Formatter('[%(levelname)s] - %(filename)s:%(lineno)s\t- %(asctime)s - %(message)s')
  
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
    { 'Param': ('-c', '--config'), 'action': 'store', 'type': 'string', 'dest': 'o_sConfigFilePath', 'default': 'config.conf', 'metavar': '<Config file path>', 'help': 'Set config file path.\t\tdefault) config.conf (contents type is JSON)' },
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