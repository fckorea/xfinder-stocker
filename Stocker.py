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
import math
from functools import reduce
import re
import requests

from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QObject
from PyQt5.QtCore import QThread
from PyQt5.QtCore import QEventLoop
from PyQt5.QtWidgets import QApplication

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


ACCOUNT_INFO = {
  'account_number': None,
  'available_money': 0,
  'my_stocks': []
}

#=============================== Worker Class ===============================#
class SellWorker(QThread):
  def run(self):
    global LOGGER
    global CONNECTION_OPTION

    LOGGER.info('<<<<< CHECK SELL >>>>>')
    

class BuyWorker(QThread):
  def run(self):
    global LOGGER
    global KIWOOM_OPTION
    global CONNECTION_OPTION
    global ACCOUNT_INFO
    global TODAY_LIST
    global TRADING_LIST
    global TRADER
    global WORKER_TERMINATE_STATUS
    global TERMINATE

    while True:
      if WORKER_TERMINATE_STATUS['sell']:
        break
      time.sleep(1)
    
    LOGGER.info('<<<<< CHECK BUY >>>>>')

    message = [ '<<< BUY LIST >>>' ]

    if TRADING_LIST['available_buy_count'] == 0 and ACCOUNT_INFO['available_money'] < KIWOOM_OPTION['money_per_buy']:
      message.append('주문 가능 금액이 설정된 최소구매금액보다 적어 구매를 할 수 없습니다.')
      message.append('')
      message.append('주문가능금액: %s원' % (fnCommify(ACCOUNT_INFO['available_money'])))
      message.append('최소구매금액: %s원' % (fnCommify(KIWOOM_OPTION['money_per_buy'])))
      fnSendMessage(message)
    else:
      # 우선주 제거
      new_list = list(filter(lambda x: x['symbol_code'][-1:] == '0', TODAY_LIST['buy']))
      # End of 우선주 제거

      for buy_stock_info in TRADING_LIST['buy'][:TRADING_LIST['available_buy_count']]:
        TRADER.trading_buy_list[buy_stock_info['symbol_code']] = {
          'name': buy_stock_info['name'],
          'trade_price': abs(buy_stock_info['trade_price']),
          'quantity': buy_stock_info['quantity']
        }
      
      if len(TRADER.trading_buy_list.keys()) == 0:
        message.append('매수할 종목이 없습니다.')
        message.append('')
        message.append('Buy Signal: %s개' % (fnCommify(len(new_list))))
        message.append('보유주식수: %s개' % (fnCommify(len(ACCOUNT_INFO['my_stocks']))))
        fnSendMessage(message)
      else:
        predict_sum = 0

        for (i, symbol_code) in enumerate(TRADER.trading_buy_list):
          message.append('===== %d / %d =====' % ((i + 1), len(TRADER.trading_buy_list.keys())))
          message.append('종목명: %s (%s)' % (TRADER.trading_buy_list[symbol_code]['name'], symbol_code))
          message.append('시가: %s' % (fnCommify(TRADER.trading_buy_list[symbol_code]['trade_price'])))
          message.append('주문수량: %s주 (시가기준)' % (fnCommify(TRADER.trading_buy_list[symbol_code]['quantity'])))
          message.append('예상주문금액: %s원' % (fnCommify(TRADER.trading_buy_list[symbol_code]['trade_price']*TRADER.trading_buy_list[symbol_code]['quantity'])))
          message.append('')
          predict_sum += TRADER.trading_buy_list[symbol_code]['trade_price']*TRADER.trading_buy_list[symbol_code]['quantity']
        message.append('')
        message.append('* 우선주 제외')
        fnSendMessage(message)

        TRADER.trading_total['buy'] = {
          'sum': 0,
          'stock_count': len(TRADER.trading_buy_list.keys()),
          'predict_sum': predict_sum
        }

        for buy_stock_info in TRADING_LIST['buy']:
          TRADER.kiwoom_SendOrder("TRADER_NEW_BUY", "1111", ACCOUNT_INFO['account_number'], 1, buy_stock_info['symbol_code'], buy_stock_info['quantity'], 0, '03', '')
          LOGGER.debug('Buy send order %s(%s)' % (buy_stock_info['name'], buy_stock_info['symbol_code']))
          time.sleep(0.3)
    
    buy_wait_count = 0

    while True:
      if len(TRADER.trading_buy_list.keys()) == 0:
        if 'buy' in TRADER.trading_total.keys():
          message = [ '<<<  매수 결과 >>>' ]
          message.append('총 매수 주식 종목: %s개' % (
            fnCommify(TRADER.trading_total['buy']['stock_count'])
          ))
          message.append('')
          message.append('총 예상 매수금액: %s원' % (
            fnCommify(TRADER.trading_total['buy']['predict_sum'])
          ))
          message.append('')
          message.append('총 매수금액: %s원' % (
            fnCommify(TRADER.trading_total['buy']['sum'])
          ))
          message.append('')
          message.append('예상 대비 추가 매수금액: %s원' % (
            fnCommify(TRADER.trading_total['buy']['sum'] - TRADER.trading_total['buy']['predict_sum'])
          ))
          fnSendMessage(message)
      
        message = [ '*** 매수 프로세스 완료 ***' ]
        fnSendMessage(message)
        break
      elif WORKER_TERMINATE_STATUS['buy']:
        message = [ '*** 매수 프로세스 에러 ***' ]
        message.append(WORKER_TERMINATE_STATUS['buy_msg'])
        fnSendMessage(message)

        message = [ '*** 매수 프로세스 완료 ***' ]
        fnSendMessage(message)
        break
      else:
        time.sleep(1)
        buy_wait_count += 1

        if buy_wait_count == CONNECTION_OPTION['waiting']:
          message = [ '!!! 매수 미완료 !!!' ]
          message.append('직접 확인 필요!!!')
          fnSendMessage(message)
          break

    if 'buy' in TRADER.trading_total.keys():
      # UPDATE ACCOUNT INFO
      fnUpdateAccountInfo()

      # AVAILABLE MONEY & MY STOCKS INFO
      fnSendAccountInfo()

    WORKER_TERMINATE_STATUS['buy'] = True
    TERMINATE = True

#=============================== Check Buy Sell Functions ===============================#
def fnCheckSellStocks():
  return

#=============================== Main Functions ===============================#
def fnMain(argOptions, argArgs):
  global LOGGER

  try:
    fnLoadingOptions()

    if fnCheckOptions() is False:
      return False
    # print(fnGetConsensusInfo())
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
      for try_count in range(CONNECTION_OPTION['try_count']):
        try:
          if target == 'buy':
            for (idx, option) in enumerate(BUY_OPTION['buy_level_option']):
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

def fnGetMoreInfoMyStock():
  global LOGGER
  global STOCKER_URL
  global CONNECTION_OPTION
  global SELL_OPTION

  LOGGER.info('Get More info from web!')

  data = None

  try:
    url = '%s/info?date=%s&symbol_code=%s' % (STOCKER_URL, datetime.today().strftime("%Y-%m-%d"), ','.join(list(map(lambda x: 'A' + x['symbol_code'], ACCOUNT_INFO['my_stocks']))))

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
      LOGGER.info('\t\tKOSPI: %.2f%%' % (SELL_OPTION['stats']['percentage']['KOSPI']['avg_profit_rate']))
      LOGGER.info('\t\tKOSDAQ: %.2f%%' % (SELL_OPTION['stats']['percentage']['KOSDAQ']['avg_profit_rate']))
    
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