#-*- coding: utf-8 -*-
#-------------------------------------------------------------------------------
# Name:        LoginChange
# Purpose:
# Python version: 3.7.3
#
# Author:    fckorea
#
# Created:    2020-05-11
# (c) fckorea 2020
#-------------------------------------------------------------------------------

import os
import sys
from optparse import OptionParser
import logging
import logging.handlers
import traceback
import time
from datetime import datetime

from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QObject
from PyQt5.QtCore import QThread
from PyQt5.QtCore import QEventLoop
from PyQt5.QtWidgets import QApplication

PROG_NAME = 'LoginChange' ### CHANGE!!!
PROG_VER = '1.0'
LOGGER = None
LOG_DIR = './logs'
LOG_FILENAME = os.path.abspath('%s/%s.log' % (LOG_DIR, PROG_NAME.replace(' ', '-').lower()))

TERMINATE = False
APP = None
TRADER = None

class TerminateWorker(QThread):
  def run(self):
    global APP
    global TERMINATE

    while(True):
      if TERMINATE == True:
        APP.quit()
      time.sleep(5)

class SyncRequestDecorator:
  """키움 API 비동기 함수 데코레이터
  """
  @staticmethod
  def kiwoom_sync_request(func):
    def func_wrapper(self, *args, **kwargs):
      global LOGGER

      if kwargs.get('nPrevNext', 0) == 0:
        LOGGER.debug('초기 요청 준비')
        self.params = {}
        self.result = {}
      # self.request_thread_worker.request_queue.append((func, args, kwargs))
      LOGGER.debug("요청 실행: %s %s %s" % (func.__name__, args, kwargs))
      func(self, *args, **kwargs)
      self.event = QEventLoop()
      self.event.exec_()
      return self.result  # 콜백 결과 반환
    return func_wrapper

  @staticmethod
  def kiwoom_sync_callback(func):
    def func_wrapper(self, *args, **kwargs):
      global LOGGER
      LOGGER.debug("요청 콜백: %s %s %s" % (func.__name__, args, kwargs))
      func(self, *args, **kwargs)  # 콜백 함수 호출
      if self.event is not None:
        self.event.exit()
    return func_wrapper

class SysTrader(QObject):
  def __init__(self):
    super().__init__()
    
    self.worker = TerminateWorker()
    self.worker.start()

    self.kiwoom = QAxWidget('KHOPENAPI.KHOpenAPICtrl.1')
    self.kiwoom.OnEventConnect.connect(self.kiwoom_OnEventConnect)
    self.kiwoom.OnReceiveTrData.connect(self.kiwoom_OnReceiveTrData)

    # 파라미터
    self.params = {}

    # Trading list
    self.trading_total = {}
    self.trading_sell_list = {}
    self.trading_buy_list = {}

    # 요청 결과
    self.event = None
    self.result = {}

  # -------------------------------------
  # 로그인 관련함수
  # -------------------------------------
  @SyncRequestDecorator.kiwoom_sync_request
  def kiwoom_CommConnect(self, **kwargs):
    """로그인 요청 (키움증권 로그인창 띄워줌. 자동로그인 설정시 바로 로그인 진행)
    OnEventConnect() 콜백
    :param kwargs:
    :return: 1: 로그인 요청 성공, 0: 로그인 요청 실패
    """
    lRet = self.kiwoom.dynamicCall("CommConnect()")
    return lRet

  def kiwoom_GetConnectState(self, **kwargs):
    """로그인 상태 확인
    OnEventConnect 콜백
    :param kwargs:
    :return: 0: 연결안됨, 1: 연결됨
    """
    lRet = self.kiwoom.dynamicCall("GetConnectState()")
    return lRet

  def kiwoom_GetAccList(self):
    """
    Get account list
    :return: accout list, in python list form.
    """
    global LOGGER

    raw = self.kiwoom.dynamicCall("GetLoginInfo(\"ACCLIST\")")
    result = raw.split(";")
    if result[-1] == '':
      result.pop()
    return result

  @SyncRequestDecorator.kiwoom_sync_callback
  def kiwoom_OnEventConnect(self, nErrCode, **kwargs):
    """로그인 결과 수신
    로그인 성공시 [조건목록 요청]GetConditionLoad() 실행
    :param nErrCode: 0: 로그인 성공, 100: 사용자 정보교환 실패, 101: 서버접속 실패, 102: 버전처리 실패
    :param kwargs:
    :return:
    """
    global LOGGER

    self.result['status'] = nErrCode

    if nErrCode == 0:
      self.result['message'] = '로그인 성공'
    elif nErrCode == 100:
      self.result['message'] = '사용자 정보교환 실패'
    elif nErrCode == 101:
      self.result['message'] = '서버접속 실패'
    elif nErrCode == 102:
      self.result['message'] = '버전처리 실패'
  
    LOGGER.debug(self.result['message'])

  # -------------------------------------
  # Data 관련함수
  # -------------------------------------
  def kiwoom_GetCommData(self, sTRCode, sRQName, nIndex, sItemName):
    """
    :param sTRCode:
    :param sRQName:
    :param nIndex:
    :param sItemName:
    :return:
    """
    res = self.kiwoom.dynamicCall("GetCommData(QString, QString, int, QString)", sTRCode, sRQName, nIndex, sItemName)
    return res

  # -------------------------------------
  # TR 관련함수
  # -------------------------------------
  def kiwoom_SetInputValue(self, sID, sValue):
    """
    :param sID:
    :param sValue:
    :return:
    """
    res = self.kiwoom.dynamicCall("SetInputValue(QString, QString)", [sID, sValue])
    return res

  def kiwoom_CommRqData(self, sRQName, sTrCode, nPrevNext, sScreenNo):
    """
    :param sRQName:
    :param sTrCode:
    :param nPrevNext:
    :param sScreenNo:
    :return:
    """
    res = self.kiwoom.dynamicCall("CommRqData(QString, QString, int, QString)", [sRQName, sTrCode, nPrevNext, sScreenNo])
    return res

  @SyncRequestDecorator.kiwoom_sync_callback
  def kiwoom_OnReceiveTrData(self, sScrNo, sRQName, sTRCode, sRecordName, sPreNext, nDataLength, sErrorCode, sMessage, sSPlmMsg, **kwargs):
    """TR 요청에 대한 결과 수신
    데이터 얻어오기 위해 내부에서 GetCommData() 호출
      GetCommData(
      BSTR strTrCode,   // TR 이름
      BSTR strRecordName,   // 레코드이름
      long nIndex,      // TR반복부
      BSTR strItemName) // TR에서 얻어오려는 출력항목이름
    :param sScrNo: 화면번호
    :param sRQName: 사용자 구분명
    :param sTRCode: TR이름
    :param sRecordName: 레코드 이름
    :param sPreNext: 연속조회 유무를 판단하는 값 0: 연속(추가조회)데이터 없음, 2:연속(추가조회) 데이터 있음
    :param nDataLength: 사용안함
    :param sErrorCode: 사용안함
    :param sMessage: 사용안함
    :param sSPlmMsg: 사용안함
    :param kwargs:
    :return:
    """
    global LOGGER
    global TERMINATE

    LOGGER.info(sRQName)

    if sRQName == "예수금상세현황요청":
      self.result['data'] = {
        'available_money': int(self.kiwoom_GetCommData(sTRCode, sRQName, 0, "주문가능금액"))
      }
      self.printData(sTRCode, sRQName, [
        "예수금",
        "주식증거금현금",
        "출금가능금액",
        "주문가능금액",
        "수익증권매수가능금액",
        "현금미수금",
        "d+1추정예수금",
        "d+1매도매수정산금",
        "d+1매수정산금",
        "d+1미수변제소요금",
        "d+1매도정산금",
        "d+1출금가능금액",
        "d+2추정예수금",
        "d+2매도매수정산금",
        "d+2매수정산금",
        "d+2미수변제소요금",
        "d+2매도정산금",
        "d+2출금가능금액"
      ])

    if sRQName == "계좌평가현황요청":
      self.result['data'] = {
        'available_money': int(self.kiwoom_GetCommData(sTRCode, sRQName, 0, "D+2추정예수금"))
      }
      self.printData(sTRCode, sRQName, [
        "예수금",
        "D+2추정예수금"
      ])

      # self.result['data'] = {}
      # self.result['data']['account_status'] = {
      #   "money": int(self.kiwoom_GetCommData(sTRCode, sRQName, 0, "예수금").strip()),
      #   "buy_mount": int(self.kiwoom_GetCommData(sTRCode, sRQName, 0, "총매입금액").strip()),
      # }
      # self.result['data']['stock_status'] = [{
      #   "name": self.kiwoom_GetCommData(sTRCode, sRQName, x, "종목명").strip(),
      #   "symbol_code": self.kiwoom_GetCommData(sTRCode, sRQName, x, "종목코드").strip(),
      #   "quantity": self.kiwoom_GetCommData(sTRCode, sRQName, x, "보유수량").strip(),
      #   "trade_price": self.kiwoom_GetCommData(sTRCode, sRQName, x, "현재가").strip(),
      #   "cur_price": self.kiwoom_GetCommData(sTRCode, sRQName, x, "평가금액").strip(),
      #   "margin_price": self.kiwoom_GetCommData(sTRCode, sRQName, x, "손익금액").strip(),
      #   "margin_rate": self.kiwoom_GetCommData(sTRCode, sRQName, x, "손익율").strip(),
      #   "buy_price": self.kiwoom_GetCommData(sTRCode, sRQName, x, "매입금액").strip(),
      #   "money": self.kiwoom_GetCommData(sTRCode, sRQName, x, "결제잔고").strip()
      # } for x in range(self.kiwoom.dynamicCall("GetRepeatCnt(QString, QString)", sTRCode, sRQName))]

      LOGGER.debug(self.result)
      
    if sRQName == "계좌수익율요청":
      data_cnt = self.kiwoom.dynamicCall("GetRepeatCnt(QString, QString)", sTRCode, sRQName)

      self.result['count'] = data_cnt
      self.result['data'] = []

      for i in range(data_cnt):
        self.result['data'].append({
          'date': self.kiwoom_GetCommData(sTRCode, sRQName, i, "일자").strip(),
          'symbol_code': self.kiwoom_GetCommData(sTRCode, sRQName, i, "종목코드").strip(),
          'name': self.kiwoom_GetCommData(sTRCode, sRQName, i, "종목명").strip(),
          'trade_price': int(self.kiwoom_GetCommData(sTRCode, sRQName, i, "현재가")),
          'buy_price': int(self.kiwoom_GetCommData(sTRCode, sRQName, i, "매입가")),
          'buy_amount': int(self.kiwoom_GetCommData(sTRCode, sRQName, i, "매입금액")),
          'quantity': int(self.kiwoom_GetCommData(sTRCode, sRQName, i, "보유수량")),
          'cur_sell_revenue': int(self.kiwoom_GetCommData(sTRCode, sRQName, i, "당일매도손익")),
          'cur_sell_fees': self.kiwoom_GetCommData(sTRCode, sRQName, i, "당일매매수수료"),
          'cur_sell_tax': int(self.kiwoom_GetCommData(sTRCode, sRQName, i, "당일매매세금")),
          'balance': int(self.kiwoom_GetCommData(sTRCode, sRQName, i, "결제잔고")),
        })
      
    if sRQName == "주식기본정보":
      info = [
        '종목코드',
        '종목명',
        '시가'
      ]

      self.result['data'] = {
        x: self.kiwoom_GetCommData(sTRCode, sRQName, 0, x).strip() for x in info
      }

      self.result['data']['시가'] = int(self.result['data']['시가'])

      LOGGER.debug(self.result)

  # -------------------------------------
  # 계좌 관련함수
  # -------------------------------------
  @SyncRequestDecorator.kiwoom_sync_request
  def kiwoom_TR_OPW00001_예수금상세현황요청(self, account, **kwargs):
    """예수금상세현황요청
    :param 계좌번호: 계좌번호
    :param kwargs:
    :return:
    """
    res = self.kiwoom_SetInputValue("계좌번호", account)
    res = self.kiwoom_CommRqData("예수금상세현황요청", "opw00001", 0,  "0362")
  
  @SyncRequestDecorator.kiwoom_sync_request
  def kiwoom_TR_OPW00004_계좌평가현황요청(self, account, **kwargs):
    """계좌평가현황요청
    :param 계좌번호: 계좌번호
    :param kwargs:
    :return:
    """
    res = self.kiwoom_SetInputValue("계좌번호", account)
    res = self.kiwoom_CommRqData("계좌평가현황요청", "OPW00004", 0,  "0362")
  
  @SyncRequestDecorator.kiwoom_sync_request
  def kiwoom_TR_OPT10085_계좌수익율요청(self, account, **kwargs):
    """계좌수익율요청
    :param 계좌번호: 계좌번호
    :param kwargs:
    :return:
    """
    res = self.kiwoom_SetInputValue("계좌번호", account)
    res = self.kiwoom_CommRqData("계좌수익율요청", "opt10085", 0,  "0345")

  # -------------------------------------
  # 종목 관련함수
  # -------------------------------------
  @SyncRequestDecorator.kiwoom_sync_request
  def kiwoom_TR_OPT10001_주식기본정보요청(self, strCode, **kwargs):
    """주식기본정보요청
    :param strCode:
    :param kwargs:
    :return:
    """
    res = self.kiwoom_SetInputValue("종목코드", strCode)
    res = self.kiwoom_CommRqData("주식기본정보", "OPT10001", 0, "0114")
    return res

#=============================== Main Functions ===============================#
def fnMain(argOptions, argArgs):
  global LOGGER
  global APP
  global TRADER

  global TERMINATE

  message = None

  try:
    LOGGER.info('<<<<< START LOGIN CHANGE! >>>>>')

    time.sleep(1)

    APP = QApplication(argArgs)
    TRADER = SysTrader()

    # LOGIN
    TRADER.kiwoom_CommConnect()
    if TRADER.kiwoom_GetConnectState() == 1:
      message = 'KIWOOM LOGIN SUCCESS!!! (%s)' % (TRADER.result['message'])
    else:
      message = 'KIWOOM LOGIN FAILED (%s)' % (TRADER.result['message'])

    LOGGER.info(message)

    # GET ACCOUNT INFO
    account_list = TRADER.kiwoom_GetAccList()
    LOGGER.info(account_list)

    APP.exec_()
    return True
  except KeyboardInterrupt:
    LOGGER.info('<<< EXIT Signal >>>')
    TERMINATE = True
  except:
    LOGGER.error(' *** Error in Main.')
    LOGGER.debug(traceback.format_exc())
    LOGGER.info(message)
  finally:
    LOGGER.info('<<<<< TERMINATE LOGIN CHANGE! >>>>>')
    return True

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

  return True

#=============================== OptionParser Functions ===============================#
def fnSetOptions():
  global PROG_VER

  parser = None

  # Ref. https://docs.python.org/2/library/optparse.html#optparse-reference-guide
  options = [
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
  return argParser.parse_args()

if __name__ == '__main__':
  (parsed_options, argvs) = fnGetOptions(fnSetOptions())
  if fnInit(parsed_options):
    LOGGER.info('Start %s...' % (PROG_NAME))
    fnMain(parsed_options, argvs)
    LOGGER.info('Terminate %s...' % (PROG_NAME))
