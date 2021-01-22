#-*- coding: utf-8 -*-
#-------------------------------------------------------------------------------
# Name:        Stocker
# Purpose:
# Python version: 3.7.3
#
# Author:    fckorea
#
# Created:    2020-07-12
# (c) fckorea 2020
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
import math
from functools import reduce
import re
import requests
import copy

import telegram

from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QObject
from PyQt5.QtCore import QThread
from PyQt5.QtCore import QEventLoop
from PyQt5.QtWidgets import QApplication

PROG_NAME = 'Stocker' ### CHANGE!!!
PROG_VER = '1.1'
LOGGER = None
LOG_DIR = './logs'
LOG_FILENAME = os.path.abspath('%s/%s.log' % (LOG_DIR, PROG_NAME.replace(' ', '-').lower()))
CONFIG = {}

SYSTEM_OPTION = {
  'auto_shutdown': False
}
KIWOOM_OPTION = {
  'money_per_buy': 250000
}
CONNECTION_OPTION = {
  'waiting': 600,
  'try_count': 3
}
BUY_OPTION = {
  'buy_level': 0,
  'buy_level_0_option': {
    'level': 4
  },
  'buy_level_1_option': {
    'level': 4
  },
  'buy_level_2_option': {
    'level': 4,
    'rate': 0.3
  }
}
SELL_OPTION = {   # Only unlisted cut
  'profit_cut': False,
  'no_more_buy_profit_cut': False,
  'profit_cut_by_stats': False,
  'target_price_cut': False,
  'minimum_profit_cut_percentage': 5
}
TELEGRAM_OPTION = {}

TERMINATE = False
APP = None
TRADER = None

LASTDAY_FILE = 'conf/lastday.conf'
LASTDAY = None

WORKER_TERMINATE_STATUS = {
  'sell': False,
  'buy': False
}

TELEGRAM_BOT = None

ACCOUNT_INFO = {
  'account_number': None,
  'available_money': 0,
  'my_stocks': []
}

TODAY_LIST = None
TRADING_LIST = {
  'sell': [],
  'buy': []
}
SELL_EXCEPTION = []

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
    self.kiwoom.OnReceiveChejanData.connect(self.kiwoom_OnReceiveChejanData)
    self.kiwoom.OnReceiveMsg.connect(self.kiwoom_OnReceiveMsg)

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
  # 테스트 관련함수
  # -------------------------------------
  def printData(self, sTRCode, sRQName, argValues):
    global LOGGER

    LOGGER.debug('PRINT DATA')

    for v in argValues:
      LOGGER.debug("%s: %s" % (v, str(self.kiwoom_GetCommData(sTRCode, sRQName, 0, v))))
  
  # -------------------------------------
  # 기타 유틸 함수
  # -------------------------------------
  def getQuantity(self, iTradePrice, iMaxMoney):
    resQuantity = 0

    iTradePrice = abs(iTradePrice)

    if iMaxMoney >= iTradePrice:
      resQuantity = math.floor(iMaxMoney / iTradePrice)

    return resQuantity
  
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

    if sRQName == "계좌평가현황요청":
      self.result['data'] = {
        'available_money': int(self.kiwoom_GetCommData(sTRCode, sRQName, 0, "D+2추정예수금"))
      }
      self.printData(sTRCode, sRQName, [
        "예수금",
        "D+2추정예수금"
      ])
      
    if sRQName == "계좌수익율요청":
      data_cnt = self.kiwoom.dynamicCall("GetRepeatCnt(QString, QString)", sTRCode, sRQName)

      self.result['count'] = data_cnt
      self.result['data'] = []

      for i in range(data_cnt):
        if int(self.kiwoom_GetCommData(sTRCode, sRQName, i, "보유수량")) == 0:
          continue

        self.result['data'].append({
          'date': self.kiwoom_GetCommData(sTRCode, sRQName, i, "일자").strip(),
          'symbol_code': self.kiwoom_GetCommData(sTRCode, sRQName, i, "종목코드").strip(),
          'name': self.kiwoom_GetCommData(sTRCode, sRQName, i, "종목명").strip(),
          'trade_price': abs(int(self.kiwoom_GetCommData(sTRCode, sRQName, i, "현재가"))),
          'buy_price': int(self.kiwoom_GetCommData(sTRCode, sRQName, i, "매입가")),
          'buy_amount': int(self.kiwoom_GetCommData(sTRCode, sRQName, i, "매입금액")),
          'quantity': int(self.kiwoom_GetCommData(sTRCode, sRQName, i, "보유수량")),
          'cur_sell_revenue': int(self.kiwoom_GetCommData(sTRCode, sRQName, i, "당일매도손익")),
          'cur_sell_fees': int(self.kiwoom_GetCommData(sTRCode, sRQName, i, "당일매매수수료")) if self.kiwoom_GetCommData(sTRCode, sRQName, i, "당일매매수수료").strip() != '' else 0,
          'cur_sell_tax': int(self.kiwoom_GetCommData(sTRCode, sRQName, i, "당일매매세금")) if self.kiwoom_GetCommData(sTRCode, sRQName, i, "당일매매세금").strip() != '' else 0,
          'balance': int(self.kiwoom_GetCommData(sTRCode, sRQName, i, "결제잔고")),
          'profit_rate': (abs(int(self.kiwoom_GetCommData(sTRCode, sRQName, i, "현재가"))) - int(self.kiwoom_GetCommData(sTRCode, sRQName, i, "매입가"))) / int(self.kiwoom_GetCommData(sTRCode, sRQName, i, "매입가"))
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

  # -------------------------------------
  # 주문 관련함수
  # OnReceiveTRData(), OnReceiveMsg(), OnReceiveChejan()
  # -------------------------------------
  @SyncRequestDecorator.kiwoom_sync_request
  def kiwoom_SendOrder(self, sRQName, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, sOrgOrderNo, **kwargs):
    """주문
    :param sRQName: 사용자 구분명
    :param sScreenNo: 화면번호
    :param sAccNo: 계좌번호 10자리
    :param nOrderType: 주문유형 1:신규매수, 2:신규매도 3:매수취소, 4:매도취소, 5:매수정정, 6:매도정정
    :param sCode: 종목코드
    :param nQty: 주문수량
    :param nPrice: 주문가격
    :param sHogaGb: 거래구분(혹은 호가구분)은 아래 참고
      00 : 지정가
      03 : 시장가
      05 : 조건부지정가
      06 : 최유리지정가
      07 : 최우선지정가
      10 : 지정가IOC
      13 : 시장가IOC
      16 : 최유리IOC
      20 : 지정가FOK
      23 : 시장가FOK
      26 : 최유리FOK
      61 : 장전시간외종가
      62 : 시간외단일가매매
      81 : 장후시간외종가
    :param sOrgOrderNo: 원주문번호입니다. 신규주문에는 공백, 정정(취소)주문할 원주문번호를 입력합니다.
    :param kwargs:
    :return:
    """
    global LOGGER

    LOGGER.debug("주문: %s %s %s %s %s %s %s %s %s" % (
    sRQName, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, sOrgOrderNo))
    lRet = self.kiwoom.dynamicCall("SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)", [
      sRQName,
      sScreenNo,
      sAccNo,
      nOrderType,
      sCode,
      nQty,
      nPrice,
      sHogaGb,
      sOrgOrderNo
      ]
    )
    LOGGER.debug("kiwoom_SendOrder.lRet: {}".format(lRet))

  def kiwoom_OnReceiveMsg(self, sScrNo, sRQName, sTrCode, sMsg, **kwargs):
    """주문성공, 실패 메시지
    :param sScrNo: 화면번호
    :param sRQName: 사용자 구분명
    :param sTrCode: TR이름
    :param sMsg: 서버에서 전달하는 메시지
    :param kwargs:
    :return:
    """
    global LOGGER
    global WORKER_TERMINATE_STATUS
    
    LOGGER.debug("주문/잔고: %s %s - %s - %s" % (sScrNo, sRQName, sTrCode, sMsg))

    if sMsg.startswith('[505217]') or sMsg.startswith('[571489]') or ' 장종료 ' in sMsg:
      if sTrCode.startswith('KOA_NORMAL_BUY_'):
        WORKER_TERMINATE_STATUS['buy'] = True
        WORKER_TERMINATE_STATUS['buy_msg'] = sMsg
      elif sTrCode.startswith('KOA_NORMAL_SELL_'):
        WORKER_TERMINATE_STATUS['sell'] = True
        WORKER_TERMINATE_STATUS['sell_msg'] = sMsg

  def kiwoom_OnReceiveChejanData(self, sGubun, nItemCnt, sFIdList, **kwargs):
    """주문접수, 체결, 잔고발생시
    :param sGubun: 체결구분 접수와 체결시 '0'값, 국내주식 잔고전달은 '1'값, 파생잔고 전달은 '4"
    :param nItemCnt:
    :param sFIdList:
    "9201" : "계좌번호"
    "9203" : "주문번호"
    "9001" : "종목코드"
    "913" : "주문상태"
    "302" : "종목명"
    "900" : "주문수량"
    "901" : "주문가격"
    "902" : "미체결수량"
    "903" : "체결누계금액"
    "904" : "원주문번호"
    "905" : "주문구분"
    "906" : "매매구분"
    "907" : "매도수구분"
    "908" : "주문/체결시간"
    "909" : "체결번호"
    "910" : "체결가"
    "911" : "체결량"
    "10" : "현재가"
    "27" : "(최우선)매도호가"
    "28" : "(최우선)매수호가"
    "914" : "단위체결가"
    "915" : "단위체결량"
    "919" : "거부사유"
    "920" : "화면번호"
    "917" : "신용구분"
    "916" : "대출일"
    "930" : "보유수량"
    "931" : "매입단가"
    "932" : "총매입가"
    "933" : "주문가능수량"
    "945" : "당일순매수수량"
    "946" : "매도/매수구분"
    "950" : "당일총매도손일"
    "951" : "예수금"
    "307" : "기준가"
    "8019" : "손익율"
    "957" : "신용금액"
    "958" : "신용이자"
    "918" : "만기일"
    "990" : "당일실현손익(유가)"
    "991" : "당일실현손익률(유가)"
    "992" : "당일실현손익(신용)"
    "993" : "당일실현손익률(신용)"
    "397" : "파생상품거래단위"
    "305" : "상한가"
    "306" : "하한가"
    :param kwargs:
    :return:
    """
    global LOGGER

    LOGGER.debug("체결/잔고: %s %s %s" % (sGubun, nItemCnt, sFIdList))

    if sGubun == '0':
      list_item_name = [
        "계좌번호",
        "주문번호",
        "관리자사번",
        "종목코드",
        "주문업무분류",
        "주문상태",
        "종목명",
        "주문수량",
        "주문가격",
        "미체결수량",
        "체결누계금액",
        "원주문번호",
        "주문구분",
        "매매구분",
        "매도수구분",
        "주문체결시간",
        "체결번호",
        "체결가",
        "체결량",
        "현재가",
        "매도호가",
        "매수호가",
        "단위체결가",
        "단위체결량",
        "당일매매수수료",
        "당일매매세금",
        "거부사유",
        "화면번호",
        "터미널번호",
        "신용구분",
        "대출일"
      ]
      list_item_id = [9201, 9203, 9205, 9001, 912, 913, 302, 900, 901, 902, 903, 904, 905, 906, 907, 908, 909, 910, 911, 10, 27, 28, 914, 915, 938, 939, 919, 920, 921, 922, 923]
      parsed_data = {item_name: self.kiwoom_GetChejanData(item_id).strip() for item_name, item_id in zip(list_item_name, list_item_id)}
      
      LOGGER.debug("체결: %s" % (parsed_data,))

      # 종목코드에서 'A' 제거
      symbol_code = parsed_data["종목코드"]
      if 'A' <= symbol_code[0] <= 'Z' or 'a' <= symbol_code[0] <= 'z':
        symbol_code = symbol_code[1:]
      
      if parsed_data['주문상태'] == '체결':
        if "매도" in parsed_data['주문구분'] and int(parsed_data['미체결수량']) == 0:
          if symbol_code not in self.trading_sell_list:
            LOGGER.debug('%s is not found in trading_sell_list' % (symbol_code))
          else:
            message = [ '[ SELL ]' ]
            message.append('종목명: %s (%s)' % (
              self.trading_sell_list[symbol_code]['name'],
              symbol_code
            ))
            message.append('')
            message.append('매수평균가: %s원' % (
              fnCommify(self.trading_sell_list[symbol_code]['buy_price'])
            ))
            message.append('총 매수금액: %s원' % (
              fnCommify(self.trading_sell_list[symbol_code]['buy_amount'])
            ))
            message.append('')
            message.append('매도수량: %s주 (주문수량: %s주)' % (
              fnCommify(int(parsed_data['체결량'])),
              fnCommify(self.trading_sell_list[symbol_code]['quantity'])
            ))
            message.append('매도평균가: %s원 (주문시장가: %s원)' % (
              fnCommify(int(parsed_data['체결가'])),
              fnCommify(self.trading_sell_list[symbol_code]['trade_price'])
            ))
            message.append('총 매도금액: %s원 (주문시장가: %s원)' % (
              fnCommify(int(parsed_data['체결누계금액'])),
              fnCommify(
                self.trading_sell_list[symbol_code]['trade_price'] * self.trading_sell_list[symbol_code]['quantity']
              )
            ))
            message.append('')
            message.append('** 수익: %s원 (주당: %s원) (%.2f%%)' % (
              fnCommify(int(parsed_data['체결누계금액']) - self.trading_sell_list[symbol_code]['buy_amount']),
              fnCommify(int(parsed_data['체결가']) - self.trading_sell_list[symbol_code]['buy_price']),
              ((int(parsed_data['체결누계금액']) - self.trading_sell_list[symbol_code]['buy_amount']) / self.trading_sell_list[symbol_code]['buy_amount']) * 100
            ))
            message.append('- 수수료 및 세금에 따라 상이할 수 있음')
            fnSendMessage(message)

            self.trading_total['sell']['sum'] += int(parsed_data['체결누계금액'])
            self.trading_total['sell']['earning'] += int(parsed_data['체결누계금액']) - self.trading_sell_list[symbol_code]['buy_amount']

            del self.trading_sell_list[symbol_code]

        elif "매수" in parsed_data['주문구분'] and int(parsed_data['미체결수량']) == 0:
          if symbol_code not in self.trading_buy_list:
            LOGGER.debug('%s is not found in trading_buy_list' % (symbol_code))
          else:
            message = [ '[ BUY ]' ]
            message.append('종목명: %s (%s)' % (
              self.trading_buy_list[symbol_code]['name'],
              symbol_code
            ))
            message.append('매수수량: %s주 (주문수량: %s주)' % (
              fnCommify(int(parsed_data['체결량'])),
              fnCommify(self.trading_buy_list[symbol_code]['quantity'])
            ))
            message.append('매수평균가: %s원 (주문시장가: %s원)' % (
              fnCommify(int(parsed_data['체결가'])),
              fnCommify(self.trading_buy_list[symbol_code]['trade_price'])
            ))
            message.append('총 매수금액: %s원 (주문시장가: %s원)' % (
              fnCommify(int(parsed_data['체결누계금액'])),
              fnCommify(
                self.trading_buy_list[symbol_code]['trade_price'] * self.trading_buy_list[symbol_code]['quantity'])
            ))
            message.append('추가 매수금: %s원' % (
              fnCommify(int(parsed_data['체결누계금액']) - (self.trading_buy_list[symbol_code]['trade_price'] * self.trading_buy_list[symbol_code]['quantity']))
            ))
            fnSendMessage(message)

            self.trading_total['buy']['sum'] += int(parsed_data['체결누계금액'])

            del self.trading_buy_list[symbol_code]

    if sGubun == '1':
      list_item_name = [
        "계좌번호",
        "종목코드",
        "신용구분",
        "대출일",
        "종목명",
        "현재가",
        "보유수량",
        "매입단가",
        "총매입가",
        "주문가능수량",
        "당일순매수량",
        "매도매수구분",
        "당일총매도손일",
        "예수금",
        "매도호가",
        "매수호가",
        "기준가",
        "손익율",
        "신용금액",
        "신용이자",
        "만기일",
        "당일실현손익",
        "당일실현손익률",
        "당일실현손익_신용",
        "당일실현손익률_신용",
        "담보대출수량",
        "기타"
      ]
      list_item_id = [9201, 9001, 917, 916, 302, 10, 930, 931, 932, 933, 945, 946, 950, 951, 27, 28, 307, 8019, 957, 958, 918, 990, 991, 992, 993, 959, 924]
      parsed_data = {item_name: self.kiwoom_GetChejanData(item_id).strip() for item_name, item_id in zip(list_item_name, list_item_id)}

      # 종목코드에서 'A' 제거
      symbol_code = parsed_data["종목코드"]
      if 'A' <= symbol_code[0] <= 'Z' or 'a' <= symbol_code[0] <= 'z':
        symbol_code = symbol_code[1:]
        parsed_data["종목코드"] = symbol_code

      LOGGER.debug("잔고: %s" % (parsed_data,))

  def kiwoom_GetChejanData(self, nFid):
    """
    OnReceiveChejan()이벤트 함수가 호출될때 체결정보나 잔고정보를 얻어오는 함수입니다.
    이 함수는 반드시 OnReceiveChejan()이벤트 함수가 호출될때 그 안에서 사용해야 합니다.
    :param nFid: 실시간 타입에 포함된FID
    :return:
    """
    res = self.kiwoom.dynamicCall("GetChejanData(int)", [nFid])
    return res

#=============================== Worker Class ===============================#
class SellWorker(QThread):
  def run(self):
    global LOGGER
    global CONNECTION_OPTION
    global ACCOUNT_INFO
    global TODAY_LIST
    global TRADING_LIST
    global TRADER
    global WORKER_TERMINATE_STATUS

    LOGGER.info('<<<<< CHECK SELL >>>>>')
    
    message = [ '<<< SELL LIST >>>' ]
    
    unlisted_symbol_code = list(map(lambda x: x['symbol_code'][1:], TODAY_LIST['sell']))
    
    for sell_stock_info in TRADING_LIST['sell']:
      TRADER.trading_sell_list[sell_stock_info['symbol_code']] = {
        'name': sell_stock_info['name'],
        'buy_price': sell_stock_info['buy_price'],
        'buy_amount': sell_stock_info['buy_amount'],
        'trade_price': sell_stock_info['trade_price'],
        'quantity': sell_stock_info['quantity'],
        'profit_rate': sell_stock_info['profit_rate'],
        'sell_type': sell_stock_info['sell_type']
      }

    if len(TRADER.trading_sell_list.keys()) == 0:
      message.append('매도할 종목이 없습니다.')
      message.append('')
      message.append('Sell Signal: %s개' % (fnCommify(len(TODAY_LIST['sell']))))
      message.append('보유주식수: %s개' % (fnCommify(len(ACCOUNT_INFO['my_stocks']))))
      
      fnSendMessage(message)
    else:
      predict_sum = 0
      predict_earning = 0
      buy_sum = 0

      for (i, symbol_code) in enumerate(TRADER.trading_sell_list):
        message.append('===== %d / %d =====' % ((i + 1), len(TRADER.trading_sell_list.keys())))
        message.append('종목명: %s (%s)' % (TRADER.trading_sell_list[symbol_code]['name'], symbol_code))
        message.append('매도기준: %s' % (','.join(TRADER.trading_sell_list[symbol_code]['sell_type'])))
        message.append('시가: %s원' % (fnCommify(TRADER.trading_sell_list[symbol_code]['trade_price'])))
        message.append('주문수량: %s주' % (fnCommify(TRADER.trading_sell_list[symbol_code]['quantity'])))
        message.append('예상수익: %s원 (%.2f%%)' % (
          fnCommify((TRADER.trading_sell_list[symbol_code]['trade_price'] * TRADER.trading_sell_list[symbol_code]['quantity']) - 
          TRADER.trading_sell_list[symbol_code]['buy_amount']),
          (((TRADER.trading_sell_list[symbol_code]['trade_price'] * TRADER.trading_sell_list[symbol_code]['quantity']) - 
          TRADER.trading_sell_list[symbol_code]['buy_amount']) / TRADER.trading_sell_list[symbol_code]['buy_amount']) * 100
        ))
        predict_sum += (TRADER.trading_sell_list[symbol_code]['trade_price'] * TRADER.trading_sell_list[symbol_code]['quantity'])
        predict_earning += ((TRADER.trading_sell_list[symbol_code]['trade_price'] * TRADER.trading_sell_list[symbol_code]['quantity']) - TRADER.trading_sell_list[symbol_code]['buy_amount'])
        buy_sum += TRADER.trading_sell_list[symbol_code]['buy_amount']
        message.append('')
      
      fnSendMessage(message)

      TRADER.trading_total['sell'] = {
        'sum': 0,
        'earning': 0,
        'stock_count': len(TRADER.trading_sell_list.keys()),
        'predict_sum': predict_sum,
        'predict_earning': predict_earning,
        'buy_sum': buy_sum
      }

      for sell_stock_info in TRADING_LIST['sell']:
        TRADER.kiwoom_SendOrder("TRADER_SELL", "7777", ACCOUNT_INFO['account_number'], 2, sell_stock_info['symbol_code'], sell_stock_info['quantity'], 0, '03', '')
        time.sleep(0.3)
    
    sell_wait_count = 0

    while True:
      if len(TRADER.trading_sell_list.keys()) == 0:
        if 'sell' in TRADER.trading_total.keys():
          message = [ '<<<  매도 결과 >>>' ]
          message.append('총 매도 주식 종목: %s개' % (
            fnCommify(TRADER.trading_total['sell']['stock_count'])
          ))
          message.append('')
          message.append('총 예상 매도금액: %s원' % (
            fnCommify(TRADER.trading_total['sell']['predict_sum'])
          ))
          message.append('총 예상 수익: %s원 (%.2f%%)' % (
            fnCommify(TRADER.trading_total['sell']['predict_earning']),
            (TRADER.trading_total['sell']['predict_earning'] / TRADER.trading_total['sell']['buy_sum']) * 100
          ))
          message.append('')
          message.append('총 매도금액: %s원' % (
            fnCommify(TRADER.trading_total['sell']['sum'])
          ))
          message.append('총 수익: %s원 (%.2f%%)' % (
            fnCommify(TRADER.trading_total['sell']['sum'] - TRADER.trading_total['sell']['buy_sum']),
            (TRADER.trading_total['sell']['earning'] / TRADER.trading_total['sell']['buy_sum']) * 100
          ))
          message.append('')
          message.append('예상 대비 추가수익: %s원' % (
            fnCommify(TRADER.trading_total['sell']['sum'] - TRADER.trading_total['sell']['predict_sum'])
          ))
          fnSendMessage(message)
      
        message = [ '*** 매도 프로세스 완료 ***' ]
        fnSendMessage(message)
        break
      elif WORKER_TERMINATE_STATUS['sell']:
        message = [ '*** 매도 프로세스 에러 ***' ]
        message.append(WORKER_TERMINATE_STATUS['sell_msg'])
        fnSendMessage(message)

        message = [ '*** 매도 프로세스 완료 ***' ]
        fnSendMessage(message)
        break
      else:
        time.sleep(1)
        sell_wait_count += 1

        if sell_wait_count == CONNECTION_OPTION['waiting']:
          message = [ '!!! 매도 미완료 !!!' ]
          message.append('직접 확인 필요!!!')
          fnSendMessage(message)
          break
    
    if 'sell' in TRADER.trading_total.keys():
      # UPDATE ACCOUNT INFO
      fnUpdateAccountInfo()

      # AVAILABLE MONEY & MY STOCKS INFO
      fnSendAccountInfo()

    WORKER_TERMINATE_STATUS['sell'] = True

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

#=============================== Telegram Functions ===============================#
def fnSendMessage(argMessage):
  global LOGGER
  global CONNECTION_OPTION
  global TELEGRAM_OPTION
  global TELEGRAM_BOT

  if TELEGRAM_BOT is None:
    LOGGER.error('TELEGRAM BOT IS NONE!!!')
    return

  if argMessage == '' or len(argMessage) == 0 or (type(argMessage) is list and len(''.join(argMessage).strip()) == 0):
    LOGGER.info('Message is None!')
    return

  try:
      message = argMessage
      if type(argMessage) is list:
        message = '\n'.join(argMessage)
      LOGGER.info(message)
      for i in range(CONNECTION_OPTION['try_count']):
        try:
          TELEGRAM_BOT.sendMessage(chat_id=TELEGRAM_OPTION['chat_id'], text=message)
          break
        except:
          LOGGER.error(traceback.format_exc())
          LOGGER.error('TELEGRAM SEND MESSAGE ERROR -x- Retry: %d / %d' % (
            (i + 1),
            CONNECTION_OPTION['try_count']
          ))
  except:
    LOGGER.error(traceback.format_exc())

def fnSendConsensusInfo():
  global LOGGER
  global TODAY_LIST

  message = []

  message.append('<<< 매도 예정 종목 >>>')
  
  if len(TODAY_LIST['sell']) == 0:
    message.append('매도 종목 없음.')

  for (i, info) in enumerate(TODAY_LIST['sell']):
    message.append('===== %d / %d =====' % (
      (i + 1),
      len(TODAY_LIST['sell'])
    ))
    message.append('종목명: %s (%s, %s, %s위)' % (
      info['name'],
      info['symbol_code'],
      info['market'],
      fnCommify(info['market_rank'])
    ))
    message.append('Signal 기간: %s ~ %s' % (
      info['first_date'],
      info['last_date']
    ))
    message.append('LYR: %.2f / 5.0' % (
      info['lyr']
    ))
    message.append('목표가: %s원' % (
      fnCommify(info['target_price'])
    ))
    message.append('최근가: %s원' % (
      fnCommify(info['trade_price'])
    ))
    message.append('')
    message.append('시작가: %s원 (%s)' % (
      fnCommify(info['first_date_trade_price']),
      info['first_date']
    ))
    message.append('종료가: %s원 (%s)' % (
      fnCommify(info['last_date_trade_price']),
      info['last_date']
    ))
    message.append('> 예상 수익: %.2f%% (%s원)' % (
      (((info['last_date_trade_price'] - info['first_date_trade_price']) / info['first_date_trade_price']) * 100),
      fnCommify(info['last_date_trade_price'] - info['first_date_trade_price'])
    ))
    message.append('')
    message.append('** 최근가 예상 수익: %.2f%% (%s원)' % (
      (((info['trade_price'] - info['first_date_trade_price']) / info['first_date_trade_price']) * 100),
      fnCommify(info['trade_price'] - info['first_date_trade_price'])
    ))
    message.append('==========')
    message.append('')

  fnSendMessage(message)

  message = []

  message.append('<<< 매수 예정 종목 >>>')

  if len(TODAY_LIST['buy']) == 0:
    message.append('매수 종목 없음.')

  for (i, info) in enumerate(TODAY_LIST['buy']):
    message.append('===== %d / %d =====' % ((i + 1), len(TODAY_LIST['buy'])))
    if 'buy_level' in info:
      message.append('<< 공격적 매수 종목(Level: %d) >>' % (info['buy_level']))
    message.append('종목명: %s (%s, %s, %s위)' % (
      info['name'],
      info['symbol_code'],
      info['market'],
      fnCommify(info['market_rank'])
    ))
    message.append('Signal Date: %s' % (
      info['consensus_date']
    ))
    message.append('LYR: %.2f / 5.0' % (
      info['lyr']
    ))
    message.append('목표가: %s' % (
      fnCommify(info['target_price'])
    ))
    message.append('최근가: %s원' % (
      fnCommify(info['trade_price'])
    ))
    message.append('')
    if 'target_price' not in info or info['target_price'] is None:
      LOGGER.error('info target price Error')
      message.append('최근가 대비: -')
    elif 'trade_price' not in info or info['trade_price'] is None:
      LOGGER.error('info trade price Error')
      message.append('최근가 대비: -')
    else:
      message.append('최근가 대비: %.2f%%' % (
        (((info['trade_price'] - info['target_price']) / info['target_price']) * 100)
      ))
    message.append('==========')
    message.append('')
  
  fnSendMessage(message)

def fnSendAccountInfo():
  fnSendAccountMoney()
  fnSendMyStocksInfo()

def fnSendAccountMoney():
  global LOGGER
  global ACCOUNT_INFO

  message = [ '<<< 예수금 >>>' ]
  message.append('계좌번호: %s' % (ACCOUNT_INFO['account_number']))
  message.append('매수 가능 금액: %s원' % (fnCommify(ACCOUNT_INFO['available_money'])))
  message.append('')
  
  fnSendMessage(message)

def fnSendMyStocksInfo():
  global LOGGER
  global ACCOUNT_INFO

  message = [ '<<< 보유 주식 >>>' ]

  if len(ACCOUNT_INFO['my_stocks']) == 0:
    message.append('보유 주식 없음')
  else:
    for (i, stock) in enumerate(ACCOUNT_INFO['my_stocks']):
      message.append('===== %d / %d =====' % ((i + 1), len(ACCOUNT_INFO['my_stocks'])))
      message.append('일자: %s' % (stock['date']))
      if 'market' in stock:
        message.append('종목명: %s (%s, %s, %s위)' % (
          stock['name'],
          stock['symbol_code'],
          stock['market'],
          fnCommify(stock['market_rank'])
        ))
      else:
        message.append('종목명: %s (%s)' % (
          stock['name'],
          stock['symbol_code']
        ))
      message.append('현재가: %s원' % (fnCommify(abs(stock['trade_price']))))
      message.append('매입가: %s원' % (fnCommify(stock['buy_price'])))
      message.append('매입금액: %s원' % (fnCommify(stock['buy_amount'])))
      message.append('보유수량: %s주' % (fnCommify(stock['quantity'])))
      message.append('평가금액: %s원' % (fnCommify((abs(stock['trade_price'])*stock['quantity']))))
      message.append('현재수익: %s원 (%s%%)' % (fnCommify((abs(stock['trade_price'])*stock['quantity']) - (stock['buy_price']*stock['quantity'])), fnCommify((((abs(stock['trade_price'])*stock['quantity']) - (stock['buy_price']*stock['quantity'])) / (stock['buy_price']*stock['quantity']))*100)))
      if (i + 1) % 5 == 0:
        fnSendMessage(message)
        message = []
      else:
        message.append('')
  
  fnSendMessage(message)

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

      if(res.status_code is 200):
        break
    except:
      LOGGER.error('\t -x- Requests error:fnGetData() (Try: %02d / %02d)' % ((try_count + 1), argTryCount))
      time.sleep(1)

  if((try_count == argTryCount) or ((res is not None) and (res.status_code is not 200))):
    LOGGER.error('\t * data collecting error! (URL: %s, code: %s)' % (argURL, res.status_code))
    return None
  
  return res

#=============================== Consensus Functions ===============================#
def fnGetConsensusInfo():
  global LOGGER
  global BUY_OPTION
  global CONNECTION_OPTION

  LOGGER.info('Get Consensus from web!')

  data = {
    "unlisted": [],
    "new": []
  }

  try:
    url = 'http://tbx.kr/api/v1/trader/consensus/%s/%s'

    today = datetime.today().strftime("%Y-%m-%d")

    for target in ['unlisted', 'new']:
      for try_count in range(CONNECTION_OPTION['try_count']):
        try:
          params = { "lyr": BUY_OPTION['buy_level_0_option']['level'] } if target == 'new' else {}
          res = fnGetData((url % (target, today)), params=params)
          data[target] = res.json()['data']['rows']
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

  LOGGER.info('Get Consensus latest info from web!')

  data = None

  try:
    url = 'http://tbx.kr/api/v1/trader/consensus/lastday'

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
  global CONNECTION_OPTION
  global SELL_OPTION

  LOGGER.info('Get Profit cut info from web!')

  data = None

  try:
    url = 'http://tbx.kr/api/v1/trader/consensus/stats?days=%d' % SELL_OPTION['profit_cut_by_stats_days']

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
  global CONNECTION_OPTION
  global SELL_OPTION

  LOGGER.info('Get More info from web!')

  data = None

  try:
    url = 'http://tbx.kr/api/v1/trader/consensus/info?date=%s&symbol_code=%s' % (datetime.today().strftime("%Y-%m-%d"), ','.join(list(map(lambda x: 'A' + x['symbol_code'], ACCOUNT_INFO['my_stocks']))))

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

def fnGetAttackingBuyList(argLevel=1):
  global LOGGER
  global BUY_OPTION
  global CONNECTION_OPTION

  LOGGER.info('Get Attacking Buy Info from web!')

  data = {}

  try:
    url = 'http://tbx.kr/api/v1/trader/consensus/change/%s'

    today = datetime.today().strftime("%Y-%m-%d")

    for buy_level in range(1, argLevel + 1):
      if buy_level == 1:
        params = {
          'type': 'lyr',
          'lyr': BUY_OPTION['buy_level_1_option']['level']
        }

      if buy_level == 2:
        params = {
          'type': 'target_price',
          'lyr': BUY_OPTION['buy_level_2_option']['level'],
          'rate': BUY_OPTION['buy_level_2_option']['rate']
        }

      for try_count in range(CONNECTION_OPTION['try_count']):
        try:
          res = fnGetData((url % (today)), params=params)
          data[buy_level] = res.json()['data']['rows']
          list(map(lambda x: x.update({ 'buy_level': buy_level }), data[buy_level]))
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

#=============================== Buy Sell Functions ===============================#
def fnCheckBuySellStocks():
  global LOGGER
  global KIWOOM_OPTION
  global CONNECTION_OPTION
  global SELL_OPTION
  global TRADER
  global ACCOUNT_INFO
  global TODAY_LIST
  global SELL_EXCEPTION
  global TRADING_LIST

  # Make Buy List
  TRADING_LIST['buy'] = []

  # 우선주 제거
  new_list = list(filter(lambda x: x['symbol_code'][-1:] == '0', TODAY_LIST['buy']))
  # End of 우선주 제거

  # 중복 종목 제거
  new_list_symbolcode = []
  buy_list = []

  for new_stock in new_list:
    if new_stock['symbol_code'] not in new_list_symbolcode:
      buy_list.append(new_stock)
      new_list_symbolcode = new_stock['symbol_code']
  
  new_list = buy_list
  # End of 중복 종목 제거

  LOGGER.debug('NEW_LIST: %s' % (new_list))

  my_stock_symbol_code = list(map(lambda x: x['symbol_code'], ACCOUNT_INFO['my_stocks']))

  for new_stock in new_list:
    check_buy = False

    symbol_code = new_stock['symbol_code'][1:]

    if symbol_code not in my_stock_symbol_code:
      check_buy = True
    
    if check_buy:
      for try_count in range(CONNECTION_OPTION['try_count']):
        TRADER.kiwoom_TR_OPT10001_주식기본정보요청(symbol_code)
        time.sleep(0.1)
        buy_stock_info = TRADER.result['data']

        LOGGER.debug(buy_stock_info)

        if abs(buy_stock_info['시가']) == 0:
          LOGGER.debug('trade price is %d' % (abs(buy_stock_info['시가'])))
          LOGGER.debug('retry -x- %d / %d' % (try_count + 1, CONNECTION_OPTION['try_count']))
          time.sleep(1)
          if try_count + 1 == CONNECTION_OPTION['try_count']:
            LOGGER.debug('trade price is 0, end.')
            message = [ '<<< SKIP (Try: %d / %d) >>>' % (try_count + 1, CONNECTION_OPTION['try_count']) ]
            message.append('%s(%s) trade price is 0.' % (buy_stock_info['종목명'], buy_stock_info['종목코드']))
            fnSendMessage(message)
          continue
      
        buy_quantity = TRADER.getQuantity(abs(buy_stock_info['시가']), KIWOOM_OPTION['money_per_buy'])

        TRADING_LIST['buy'].append({
          'symbol_code': buy_stock_info['종목코드'],
          'name': buy_stock_info['종목명'],
          'trade_price': abs(buy_stock_info['시가']),
          'quantity': buy_quantity
        })
        break
      time.sleep(0.5)
    
  LOGGER.debug('TRADING_LIST[\'buy\']: %s' % (TRADING_LIST['buy']))
  LOGGER.debug('TRADING_LIST[\'sell\']: %s' % (TRADING_LIST['sell']))
  
  # Check
  # Available Buy count
  available_buy_count = math.floor(ACCOUNT_INFO['available_money'] / KIWOOM_OPTION['money_per_buy'])

  TRADING_LIST['sell'] = []
  # 1. UNLISTED LIST
  unlisted_symbol_code = list(map(lambda x: x['symbol_code'][1:], TODAY_LIST['unlisted']))
  
  if 'minimum_profit_cut_percentage' in SELL_OPTION:
    sell_unlisted = list(map(lambda x: x['symbol_code'], list(filter(lambda x: x['symbol_code'] in unlisted_symbol_code and x['quantity'] > 0 and (x['profit_rate'] * 100) >= SELL_OPTION['minimum_profit_cut_percentage'], ACCOUNT_INFO['my_stocks']))))
  else:
    sell_unlisted = list(map(lambda x: x['symbol_code'], list(filter(lambda x: x['symbol_code'] in unlisted_symbol_code and x['quantity'] > 0, ACCOUNT_INFO['my_stocks']))))

  sell_unlisted = list(filter(lambda x: x not in SELL_EXCEPTION, sell_unlisted))

  # 2. PROFIT_CUT LIST
  sell_profit_cut = []
  if SELL_OPTION['profit_cut'] is True:
    sell_profit_cut = list(map(lambda x: x['symbol_code'], list(filter(lambda x: (x['profit_rate'] * 100) >= SELL_OPTION['profit_cut_percentage'], ACCOUNT_INFO['my_stocks']))))
  
  sell_profit_cut = list(filter(lambda x: x not in SELL_EXCEPTION, sell_profit_cut))

  # 3. PROFIT_CUT_BY_STATS
  sell_profit_cut_by_stats = []
  if SELL_OPTION['profit_cut_by_stats'] is True:
    sell_profit_cut_by_stats = list(map(lambda x: x['symbol_code'], list(filter(lambda x: 'market' in x and ((x['profit_rate'] * 100) >= SELL_OPTION['profit_cut_by_stats_percentage'][x['market']]['avg_profit_rate']), ACCOUNT_INFO['my_stocks']))))
  
  sell_profit_cut_by_stats = list(filter(lambda x: x not in SELL_EXCEPTION, sell_profit_cut_by_stats))
  
  # 4. TARGET_PRICE_CUT
  sell_target_price_cut = []
  if SELL_OPTION['target_price_cut'] is True:
    if 'minimum_profit_cut_percentage' in SELL_OPTION:
      sell_target_price_cut = list(map(lambda x: x['symbol_code'], list(filter(lambda x: 'target_price' in x and (x['trade_price'] >= x['target_price']) and (x['profit_rate'] * 100) >= SELL_OPTION['minimum_profit_cut_percentage'], ACCOUNT_INFO['my_stocks']))))
    else:
      sell_target_price_cut = list(map(lambda x: x['symbol_code'], list(filter(lambda x: 'target_price' in x and (x['trade_price'] >= x['target_price']), ACCOUNT_INFO['my_stocks']))))
  
  sell_target_price_cut = list(filter(lambda x: x not in SELL_EXCEPTION, sell_target_price_cut))
  
  # 5. MORE PROFIT_CUT LIST
  sell_more_profit_cut = []
  if SELL_OPTION['no_more_buy_profit_cut'] is True:
    sell_more_profit_cut = list(map(lambda x: x['symbol_code'], list(filter(lambda x: (x['profit_rate'] * 100) >= SELL_OPTION['no_more_buy_profit_cut_percentage'], ACCOUNT_INFO['my_stocks']))))
  
  sell_more_profit_cut = list(filter(lambda x: x not in SELL_EXCEPTION, sell_more_profit_cut))

  # unlisted + profit_cut + sell_profit_cut_by_stats + sell_target_price_cut
  sell_symbols = list(set(sell_unlisted + sell_profit_cut + sell_profit_cut_by_stats + sell_target_price_cut))
  sell_count = len(sell_symbols)
  sell_more_count = len(list(filter(lambda x: x not in sell_symbols, sell_more_profit_cut)))
  buy_count = len(TRADING_LIST['buy'])

  LOGGER.debug('COUNT:')
  LOGGER.debug('\tSELL: %d' % (sell_count))
  LOGGER.debug('\t\t1. UNLISTED: %d' % (len(sell_unlisted)))
  LOGGER.debug('\t\t2. PROFIT_CUT: %d' % (len(sell_profit_cut)))
  LOGGER.debug('\t\t3. PROFIT_CUT_BY_STATS: %d' % (len(sell_profit_cut_by_stats)))
  LOGGER.debug('\t\t4. TARGET_PRICE_CUT: %d' % (len(sell_target_price_cut)))
  LOGGER.debug('\t\t5. NO_MORE_BUY_PROFIT_CUT: %d (%d)' % (sell_more_count, len(sell_more_profit_cut)))
  LOGGER.debug('\tBUY: %d' % (buy_count))
  LOGGER.debug('\tAVA_BUY_COUNT: %d' % (available_buy_count))

  # not sell more.
  if (sell_count >= buy_count) or ((available_buy_count + sell_count) >= buy_count):
    # remove Sell more
    sell_more_profit_cut = []
  else:
    # filter sell more
    remain_count = sell_more_count - ((available_buy_count + sell_count) - buy_count)

    if remain_count < 0:
      remain_count = sell_more_count

    sell_symbols += list(map(lambda x: x['symbol_code'], sorted(list(filter(lambda x: x['symbol_code'] in sell_more_profit_cut, ACCOUNT_INFO['my_stocks'])), key=lambda x: x['profit_rate'])[:remain_count]))

  # Make Sell List
  TRADING_LIST['sell'] = []

  for stock in ACCOUNT_INFO['my_stocks']:
    if stock['symbol_code'] in sell_symbols:
      sell_type = []

      if stock['symbol_code'] in sell_unlisted:
        sell_type.append('UNLISTED')
      if stock['symbol_code'] in sell_profit_cut:
        sell_type.append('PROFIT_CUT(>=%.2f%%)' % (SELL_OPTION['profit_cut_percentage']))
      if stock['symbol_code'] in sell_profit_cut_by_stats:
        sell_type.append('PROFIT_CUT_BY_STATS(>=%.2f%%, %s)' % (SELL_OPTION['profit_cut_by_stats_percentage'][stock['market']]['avg_profit_rate'], stock['market']))
      if stock['symbol_code'] in sell_target_price_cut:
        sell_type.append('TARGET_PRICE_CUT(>=%s)' % (fnCommify(stock['target_price'])))
      if stock['symbol_code'] in sell_more_profit_cut:
        sell_type.append('MORE_PROFIT_CUT(>=%.2f%%)' % (SELL_OPTION['no_more_buy_profit_cut']))

      TRADING_LIST['sell'].append({
        'symbol_code': stock['symbol_code'],
        'name': stock['name'],
        'buy_price': stock['buy_price'],
        'buy_amount': stock['buy_amount'],
        'trade_price': stock['trade_price'],
        'quantity': stock['quantity'],
        'profit_rate': stock['profit_rate'],
        'sell_type': sell_type
      })

  LOGGER.debug('TRADING_LIST[\'buy\']: %s' % (TRADING_LIST['buy']))
  LOGGER.debug('TRADING_LIST[\'sell\']: %s' % (TRADING_LIST['sell']))

  sell_to_buy_count = 0

  if len(TRADING_LIST['sell']) != 0:
    sell_to_buy_count = math.floor(reduce(lambda acc, cur: acc + (cur['trade_price'] * cur['quantity']), TRADING_LIST['sell'], 0) / KIWOOM_OPTION['money_per_buy'])

  # Setting buy list
  if len(TRADING_LIST['buy']) > (sell_to_buy_count + available_buy_count):
    TRADING_LIST['buy'] = TRADING_LIST['buy'][:(sell_to_buy_count + available_buy_count)]
    LOGGER.debug('CHANGE TRADING_LIST[\'buy\']: %s' % (TRADING_LIST['buy']))
  
  TRADING_LIST['available_buy_count'] = sell_to_buy_count + available_buy_count

#=============================== Util Functions ===============================#
def fnCommify(argValue, argPoint=2):
  if type(argValue) is int:
    return format(argValue, ',')
  if type(argValue) is float:
    return format(argValue, (',.%df' % argPoint))

#=============================== ACCOUNT_INFO Functions ===============================#
def fnUpdateAccountInfo():
  global LOGGER
  global TRADER
  global ACCOUNT_INFO

  TRADER.kiwoom_TR_OPW00004_계좌평가현황요청(ACCOUNT_INFO['account_number'])
  ACCOUNT_INFO['available_money'] = TRADER.result['data']['available_money']

  TRADER.kiwoom_TR_OPT10085_계좌수익율요청(ACCOUNT_INFO['account_number'])
    
  ACCOUNT_INFO['my_stocks'] = list(filter(lambda x: x['quantity'] != 0, TRADER.result['data']))

  more_info = fnGetMoreInfoMyStock()

  more_info_symbols = list(map(lambda x: x['symbol_code'], more_info))

  for (idx, stock) in enumerate(ACCOUNT_INFO['my_stocks']):
    if 'A' + stock['symbol_code'] in more_info_symbols:
      m_idx = more_info_symbols.index('A' + stock['symbol_code'])
      ACCOUNT_INFO['my_stocks'][idx]['market'] = more_info[m_idx]['market']
      ACCOUNT_INFO['my_stocks'][idx]['market_rank'] = more_info[m_idx]['market_rank']
      ACCOUNT_INFO['my_stocks'][idx]['level'] = more_info[m_idx]['lyr']
      ACCOUNT_INFO['my_stocks'][idx]['target_price'] = more_info[m_idx]['target_price']

#=============================== Main Functions ===============================#
def fnMain(argOptions, argArgs):
  global LOGGER
  global SYSTEM_OPTION
  global KIWOOM_OPTION
  global CONNECTION_OPTION
  global BUY_OPTION
  global SELL_OPTION
  global TELEGRAM_OPTION

  global APP
  global TRADER

  global LASTDAY
  
  global TELEGRAM_BOT
  global ACCOUNT_INFO
  global TODAY_LIST
  global SELL_EXCEPTION

  global TERMINATE

  message = None
  lastday = None

  try:
    fnLoadingOptions()

    if fnCheckOptions() is False:
      return False

    TELEGRAM_BOT = telegram.Bot(token=TELEGRAM_OPTION['token'])

    message = [ '<<<<< START STOCKER! >>>>>' ]
    fnSendMessage(message)

    if datetime.today().weekday() > 4:
      # Weekend
      message = [ '주인님, 저는 주말에 쉬어요! 😎' ]
      fnSendMessage(message)
      return False

    fnGetLastDay()
    
    message = []
    message.append('*** CONFIG ***')
    message.append('+ 거래계좌번호: %s' % (KIWOOM_OPTION['account_number']))
    message.append('+ 종목 당 매수 금액: %s원' % (fnCommify(KIWOOM_OPTION['money_per_buy'])))
    message.append('+ 매수 Level: %s' % (BUY_OPTION['buy_level']))
    message.append('+ 매수 조건')
    message.append('    - Level0: %s' % (BUY_OPTION['buy_level_0_option']['level']))
    message.append('    - Level1: %s' % (BUY_OPTION['buy_level_1_option']['level']))
    message.append('    - Level2: %s, %.2f' % (BUY_OPTION['buy_level_2_option']['level'], BUY_OPTION['buy_level_2_option']['rate'] * 100))
    message.append('+ 매도 조건')
    if SELL_OPTION['profit_cut'] is True:
      message.append('    - 익절 매도 설정: %s (>=%.2f%%)' % (SELL_OPTION['profit_cut'], SELL_OPTION['profit_cut_percentage']))
    else:
      message.append('    - 익절 매도 설정: %s' % (SELL_OPTION['profit_cut']))
    if SELL_OPTION['profit_cut_by_stats'] is True:
      message.append('    - 통계 익절 매도 설정: %s (%ddays)' % (SELL_OPTION['profit_cut_by_stats'], SELL_OPTION['profit_cut_by_stats_days']))
      for market in SELL_OPTION['profit_cut_by_stats_percentage']:
        message.append('      .%s: %.2f%%' % (market, SELL_OPTION['profit_cut_by_stats_percentage'][market]['avg_profit_rate']))
    else:
      message.append('    - 통계 익절 매도 설정: %s' % (SELL_OPTION['profit_cut_by_stats']))
    message.append('    - 목표가 매도 설정: %s' % (SELL_OPTION['target_price_cut']))
    if SELL_OPTION['no_more_buy_profit_cut'] is True:
      message.append('    - 매수금 부족 시 익절 매도 설정: %s (>=%.2f%%)' % (SELL_OPTION['no_more_buy_profit_cut'], SELL_OPTION['no_more_buy_profit_cut_percentage']))
    else:
      message.append('    - 매수금 부족 시 익절 매도 설정: %s' % (SELL_OPTION['no_more_buy_profit_cut']))
    if 'minimum_profit_cut_percentage' in SELL_OPTION:
      message.append('    - 최소 익절 매도 수익률: %.2f%%' % (SELL_OPTION['minimum_profit_cut_percentage']))
    if len(SELL_EXCEPTION) > 0:
      message.append('+ 판매 예외 종목 코드: %s' % (','.join(SELL_EXCEPTION)))
    message.append('+ 시스템 자동 종료 설정: %s' % (SYSTEM_OPTION['auto_shutdown']))

    fnSendMessage(message)

    lastday = fnGetConsensusLatestInfo()

    if LASTDAY is not None and LASTDAY == lastday:
      message = [ '*** 이미 진행한 날짜 입니다. ***' ]
      message.append('최근 진행 일자: %s' % (lastday))
      message.append('마지막으로 진행한 일자: %s' % (LASTDAY))
      
      fnSendMessage(message)
      TERMINATE = True
      return True

    TODAY_LIST = fnGetConsensusInfo()

    TODAY_LIST['sell'] = TODAY_LIST['unlisted']

    attack_buy_list = fnGetAttackingBuyList(BUY_OPTION['buy_level'])
    TODAY_LIST['attacking_buy'] = attack_buy_list

    TODAY_LIST['buy'] = TODAY_LIST['new']
    TODAY_LIST['buy'] += attack_buy_list[1] if 1 in attack_buy_list else []
    TODAY_LIST['buy'] += attack_buy_list[2] if 2 in attack_buy_list else []

    # CONSENSUS INFO
    fnSendConsensusInfo()
    # End of CONSENSUS INFO

    time.sleep(1)

    APP = QApplication(argArgs)
    TRADER = SysTrader()

    # LOGIN
    TRADER.kiwoom_CommConnect()
    if TRADER.kiwoom_GetConnectState() == 1:
      LOGGER.info('Login!!')
      message = [
        'KIWOOM LOGIN SUCCESS!!! (%s)' % (TRADER.result['message'])
      ]
      fnSendMessage(message)
    else:
      message = [
        'KIWOOM LOGIN FAILED (%s)' % (TRADER.result['message'])
      ]
      fnSendMessage(message)
      TERMINATE = True
      return True

    # GET ACCOUNT INFO
    account_list = TRADER.kiwoom_GetAccList()
    LOGGER.debug(account_list)

    if KIWOOM_OPTION['account_number'] not in account_list:
      message = [ '일치하는 계좌가 없음 (설정계좌: %s)' % (KIWOOM_OPTION['account_number']) ]
      LOGGER.debug('Not found account %s' % (KIWOOM_OPTION['account_number']))
      fnSendMessage(message)
      TERMINATE = True
      return True
    
    ACCOUNT_INFO['account_number'] = KIWOOM_OPTION['account_number']

    # UPDATE ACCOUNT INFO
    fnUpdateAccountInfo()

    # MAKE BUY SELL LIST
    fnCheckBuySellStocks()

    # AVAILABLE MONEY & MY STOCKS INFO
    fnSendAccountInfo()
    
    # SELL MY STOCK
    sell_worker = SellWorker()
    sell_worker.start()
    # END OF SELL
    
    # BUY MY STOCK
    buy_worker = BuyWorker()
    buy_worker.start()
    # END OF BUY

    APP.exec_()
    return True
  except:
    LOGGER.error(' *** Error in Main.')
    LOGGER.debug(traceback.format_exc())
    message = [ 'ERROR 발생' ]
    message.append(traceback.format_exc())
    fnSendMessage(message)
  finally:
    if lastday is not None:
      fnSetLastDay(lastday)

    if SYSTEM_OPTION['auto_shutdown']:
      os.system("shutdown -s -t 60")
      fnSendMessage('<<< RESERVATION SHUTDOWN PC >>>')
    
    fnSendMessage('<<<<< TERMINATE STOCKER! >>>>>')
    return True

#=============================== Loading & Check Option Function ===============================#
def fnLoadingOptions():
  global LOGGER
  global CONFIG
  global SYSTEM_OPTION
  global KIWOOM_OPTION
  global CONNECTION_OPTION
  global BUY_OPTION
  global SELL_OPTION
  global SELL_EXCEPTION
  global TELEGRAM_OPTION

  try:
    # Loading System Option
    if 'system_option' in CONFIG:
      SYSTEM_OPTION.update(CONFIG['system_option'])
    
    # Loading Connection Option
    if 'connection_option' in CONFIG:
      CONNECTION_OPTION.update(CONFIG['connection_option'])
    
    # Loading Telegram Option
    if 'telegram_option' in CONFIG:
      TELEGRAM_OPTION.update(CONFIG['telegram_option'])
    
    # Loading Kiwoom Option
    if 'kiwoom_option' in CONFIG:
      KIWOOM_OPTION.update(CONFIG['kiwoom_option'])
    
    # Loading Buy Option
    if 'buy_option' in CONFIG:
      BUY_OPTION.update(CONFIG['buy_option'])
    
    # Loading Sell Option
    if 'sell_option' in CONFIG:
      SELL_OPTION.update(CONFIG['sell_option'])
      SELL_OPTION['profit_cut_by_stats_percentage'] = fnGetProfitCutStats()
      
      # Check minimum_profit_cut_percentage
      if 'minimum_profit_cut_percentage' in SELL_OPTION:
        LOGGER.debug('minimum_profit_cut_percentage setted! (%.2f%%)' % (
          SELL_OPTION['minimum_profit_cut_percentage']
        ))

        # 2. set profit_cut_percentage => minimum_profit_cut_percentage
        if 'profit_cut_percentage' in SELL_OPTION and SELL_OPTION['profit_cut_percentage'] < SELL_OPTION['minimum_profit_cut_percentage']:
          LOGGER.debug('profit_cut_percentage is lower than minimum_profit_cut_percentage! (%.2f%%)' % (
            SELL_OPTION['profit_cut_percentage']
          ))
          SELL_OPTION['profit_cut_percentage'] = SELL_OPTION['minimum_profit_cut_percentage']
          LOGGER.debug('changed profit_cut_percentage! (%.2f%%)' % (
            SELL_OPTION['profit_cut_percentage']
          ))
        
        # 3. set profit_cut_percentage => minimum_profit_cut_percentage
        if 'profit_cut_by_stats_percentage' in SELL_OPTION:
          for market in SELL_OPTION['profit_cut_by_stats_percentage']:
            if SELL_OPTION['profit_cut_by_stats_percentage'][market]['avg_profit_rate'] < SELL_OPTION['minimum_profit_cut_percentage']:
              LOGGER.debug('profit_cut_by_stats_percentage[%s] is lower than minimum_profit_cut_percentage! (%.2f%%)' % (
                market,
                SELL_OPTION['profit_cut_by_stats_percentage'][market]['avg_profit_rate']
              ))
              SELL_OPTION['profit_cut_by_stats_percentage'][market]['avg_profit_rate'] = SELL_OPTION['minimum_profit_cut_percentage']
              LOGGER.debug('changed profit_cut_by_stats_percentage[%s]! (%.2f%%)' % (
                market,
                SELL_OPTION['profit_cut_by_stats_percentage'][market]['avg_profit_rate']
              ))
        
        # no more buy profit cut percentage
        if 'no_more_buy_profit_cut_percentage' in SELL_OPTION and SELL_OPTION['no_more_buy_profit_cut_percentage'] < SELL_OPTION['minimum_profit_cut_percentage']:
          LOGGER.debug('no_more_buy_profit_cut_percentage is lower than minimum_profit_cut_percentage! (%.2f%%)' % (
            SELL_OPTION['no_more_buy_profit_cut_percentage']
          ))
          SELL_OPTION['no_more_buy_profit_cut_percentage'] = SELL_OPTION['minimum_profit_cut_percentage']
          LOGGER.debug('changed no_more_buy_profit_cut_percentage! (%.2f%%)' % (
            SELL_OPTION['no_more_buy_profit_cut_percentage']
          ))

    # Loading Sell Exception
    if 'sell_exception' in CONFIG:
      LOGGER.debug('Setting the sell exception! (%s)' % (','.join(CONFIG['sell_exception'])))
      SELL_EXCEPTION = CONFIG['sell_exception']

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
      LOGGER.info('\tMoney per Buy: %s' % (KIWOOM_OPTION['money_per_buy']))
    
    # Check Buy Option
    LOGGER.info('Buy Option:')
    LOGGER.info('\tBuy Level: %d' % (BUY_OPTION['buy_level']))
    if 'buy_level_0_option' not in BUY_OPTION:
      LOGGER.info('\tBUY LEVEL 0 OPTION IS NOT SETTING!')
      res_check = False
    else:
      LOGGER.info('\tBUY LEVEL 0 OPTION: %s' % (BUY_OPTION['buy_level_0_option']))
    if 'buy_level_1_option' not in BUY_OPTION:
      LOGGER.info('\tBUY LEVEL 1 OPTION IS NOT SETTING!')
      res_check = False
    else:
      LOGGER.info('\tBUY LEVEL 1 OPTION: %s' % (BUY_OPTION['buy_level_1_option']))
    if 'buy_level_2_option' not in BUY_OPTION:
      LOGGER.info('\tBUY LEVEL 2 OPTION IS NOT SETTING!')
      res_check = False
    else:
      LOGGER.info('\tBUY LEVEL 2 OPTION: %s' % (BUY_OPTION['buy_level_2_option']))
    
    # Check Sell Option
    LOGGER.info('Sell Option:')
    # Profit Cut
    LOGGER.info('\tProfit Cut: %s' % (SELL_OPTION['profit_cut']))
    if SELL_OPTION['profit_cut'] is True and 'profit_cut_percentage' not in SELL_OPTION:
      LOGGER.info('\tPROFIT CUT IS SET, BUT PERCENTAGE IS NOT SETTING!')
      res_check = False
    elif SELL_OPTION['profit_cut'] is True and 'profit_cut_percentage' in SELL_OPTION:
      LOGGER.info('\tProfit Cut Percentage: %d%%' % (SELL_OPTION['profit_cut_percentage']))
    
    # Profit Cut by Stat
    LOGGER.info('\tProfit Cut by Stats: %s' % (SELL_OPTION['profit_cut_by_stats']))
    if SELL_OPTION['profit_cut_by_stats'] is True and 'profit_cut_by_stats_days' not in SELL_OPTION:
      LOGGER.info('\tPROFIT CUT BY STATS IS SET, BUT DAYS IS NOT SETTING!')
      res_check = False
    elif SELL_OPTION['profit_cut_by_stats'] is True and 'profit_cut_by_stats_days' in SELL_OPTION:
      LOGGER.info('\tProfit Cut by Stats Days: %d' % (SELL_OPTION['profit_cut_by_stats_days']))
    
    # Target Price Cut
    LOGGER.info('\tTarget Price Cut: %s' % (SELL_OPTION['target_price_cut']))

    # No More Buy Profit Cut
    LOGGER.info('\tNo More Buy Profit Cut: %s' % (SELL_OPTION['no_more_buy_profit_cut']))
    if SELL_OPTION['no_more_buy_profit_cut'] is True and 'no_more_buy_profit_cut_percentage' not in SELL_OPTION:
      LOGGER.info('\tNO MORE BUY PROFIT CUT IS SET, BUT PERCENTAGE IS NOT SETTING!')
      res_check = False
    elif SELL_OPTION['no_more_buy_profit_cut'] is True and 'no_more_buy_profit_cut_percentage' in SELL_OPTION:
      LOGGER.info('\tNo More Buy Profit Cut Percentage: %d%%' % (SELL_OPTION['no_more_buy_profit_cut_percentage']))

    return True
  except:
    LOGGER.info(SYSTEM_OPTION)
    LOGGER.info(CONNECTION_OPTION)
    LOGGER.info(TELEGRAM_OPTION)
    LOGGER.info(KIWOOM_OPTION)
    LOGGER.info(SELL_OPTION)
    LOGGER.debug(traceback.format_exc())
  
  return res_check

#=============================== Lastday Function ===============================#
def fnGetLastDay():
  global LOGGER
  global LASTDAY_FILE
  global LASTDAY

  try:
    if os.path.isfile(os.path.abspath(LASTDAY_FILE)):
      LASTDAY = json.loads(open(os.path.abspath(LASTDAY_FILE), encoding='UTF8').read())['lastday']
      LOGGER.info(' * Read Last-day data')
      return True
    else:
      LOGGER.error(' * Last-day file(%s) not found.' % (LASTDAY_FILE))
  except:
    LOGGER.error(' *** Error read Last-day file(%s).' % (LASTDAY_FILE))
    LOGGER.debug(traceback.format_exc())
  
  return False

def fnSetLastDay(argLastDay):
  global LOGGER
  global LASTDAY_FILE
  global LASTDAY

  try:
    with open(os.path.abspath(LASTDAY_FILE), 'w') as fp:
      json.dump({ "lastday": argLastDay }, fp, indent=4)
    LOGGER.info(' * Write Last-day data')
  except:
    LOGGER.error(' *** Error write Last-day file.')
    LOGGER.debug(traceback.format_exc())
  
  return False

#=============================== Config & Init Function ===============================#
def fnGetConfig(argConfigFilePath):
  global LOGGER
  global CONFIG

  try:
    if os.path.isfile(argConfigFilePath):
      CONFIG = json.loads(open(argConfigFilePath, encoding='UTF8').read())
      LOGGER.info(' * Read config data')
      return True
    else:
      LOGGER.error(' * Config file not found.')
  except:
    LOGGER.error(' *** Error read config file.')
    LOGGER.debug(traceback.format_exc())
  
  return False

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

  if argOptions.o_sConfigFilePath is None:
    argOptions.o_sConfigFilePath = 'conf/config.conf'

  if argOptions.o_sConfigFilePath is not None:
    LOGGER.info('Config file("%s")' % (parsed_options.o_sConfigFilePath))
    fnGetConfig(parsed_options.o_sConfigFilePath)

  return True

#=============================== OptionParser Functions ===============================#
def fnSetOptions():
  global PROG_VER

  parser = None

  # Ref. https://docs.python.org/2/library/optparse.html#optparse-reference-guide
  options = [
    { 'Param': ('-c', '--config'), 'action': 'store', 'type': 'string', 'dest': 'o_sConfigFilePath', 'metavar': '<Config file path>', 'help': 'Set config file path.\t\tdefault) conf/config.conf (contents type is JSON)' },
    { 'Param': ('-v', '--verbose'), 'action': 'store_true', 'dest': 'o_bVerbose', 'default': True, 'metavar': '<Verbose Mode>', 'help': 'Set verbose mode.\t\tdefault) True' }
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
