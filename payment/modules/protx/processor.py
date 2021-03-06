"""Prot/X Payment Gateway.

To use this module, enable it in your shop configuration, usually at http:yourshop/settings/

To override the connection urls specified below in `PROTX_DEFAULT_URLS, add a dictionary in 
your settings.py file called "PROTX_URLS", mapping the keys below to the urls you need for 
your store.  You only need to override the specific urls that have changed, the processor
will fall back to the defaults for any not specified in your dictionary.
"""
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from satchmo.configuration import config_value
from satchmo.payment.utils import record_payment
from satchmo.utils import trunc_decimal
from urllib import urlencode
import forms
import logging
import urllib2

log = logging.getLogger('protx.processor')

PROTOCOL = "2.22"

PROTX_DEFAULT_URLS = {
    'LIVE_CONNECTION' : 'https://ukvps.protx.com/vspgateway/service/vspdirect-register.vsp',
    'LIVE_CALLBACK' : 'https://ukvps.protx.com/vspgateway/service/direct3dcallback.vsp',
    'TEST_CONNECTION' : 'https://ukvpstest.protx.com/vspgateway/service/vspdirect-register.vsp',
    'TEST_CALLBACK' : 'https://ukvpstest.protx.com/vspgateway/service/direct3dcallback.vsp',
    'SIMULATOR_CONNECTION' : 'https://ukvpstest.protx.com/VSPSimulator/VSPDirectGateway.asp',
    'SIMULATOR_CALLBACK' : 'https://ukvpstest.protx.com/VSPSimulator/VSPDirectCallback.asp'
}

FORM = forms.ProtxPayShipForm

class PaymentProcessor(object):
    packet = {}
    response = {}
    
    def __init__(self, settings):
        self.settings = settings
        vendor = settings.VENDOR.value
        if vendor == "":
            log.warn('Prot/X Vendor is not set, please configure in your site configuration.')
        if settings.SIMULATOR.value:
            vendor = settings.VENDOR_SIMULATOR.value
            if not vendor:
                log.warn("You are trying to use the Prot/X VSP Simulator, but you don't have a vendor name in settings for the simulator.  I'm going to use the live vendor name, but that probably won't work.")
                vendor = settings.VENDOR.value
        
        self.packet = {
            'VPSProtocol': PROTOCOL,
            'TxType': settings.CAPTURE.value,
            'Vendor': vendor,
            'Currency': settings.CURRENCY_CODE.value,
            }
        self.valid = False

    def _url(self, key):
        urls = PROTX_DEFAULT_URLS
        if hasattr(settings, 'PROTX_URLS'):
            urls.update(settings.PROTX_URLS)

        if self.settings.SIMULATOR.value:
            key = "SIMULATOR_" + key
        else:
            if self.settings.LIVE.value:
                key = "LIVE_" + key
            else:
                key = "TEST_" + key
        return urls[key]

    def _connection(self):
        return self._url('CONNECTION')

    connection = property(fget=_connection)        
        
    def _callback(self):
        return self._url('CALLBACK')

    callback = property(fget=_callback)
    
    def log_extra(self, msg, *args):
        if self.settings.EXTRA_LOGGING.value:
            log.info("(Extra logging) " + msg, *args)
    
    def prepareData(self, data):
        try:
            cc = data.credit_card
            balance = trunc_decimal(data.balance, 2)
            self.packet['VendorTxCode'] = data.id
            self.packet['Amount'] = balance
            self.packet['Description'] = 'Online purchase'
            self.packet['CardType'] = cc.credit_type
            self.packet['CardHolder'] = cc.card_holder
            self.packet['CardNumber'] = cc.decryptedCC
            self.packet['ExpiryDate'] = '%02d%s' % (cc.expire_month, str(cc.expire_year)[2:])
            if cc.start_month is not None:
                self.packet['StartDate'] = '%02d%s' % (cc.start_month, str(cc.start_year)[2:])
            if cc.ccv is not None and cc.ccv != "":
                self.packet['CV2'] = cc.ccv
            if cc.issue_num is not None and cc.issue_num != "":
                self.packet['IssueNumber'] = cc.issue_num #'%02d' % int(cc.issue_num)
            addr = [data.bill_street1, data.bill_street2, data.bill_city, data.bill_state]
            self.packet['BillingAddress'] = ', '.join(addr)
            self.packet['BillingPostCode'] = data.bill_postal_code
        except Exception, e:
            log.error('preparing data, got error: %s\nData: %s', e, data)
            self.valid = False
            return
            
        # handle pesky unicode chars in names
        for key, value in self.packet.items():
            try:
                value = value.encode('utf-8')
                self.packet[key] = value
            except AttributeError:
                pass
            
        self.postString = urlencode(self.packet)
        self.url = self.connection
        self.order = data
        self.valid = True
    
    def prepareData3d(self, md, pares):
        self.packet = {}
        self.packet['MD'] = md
        self.packet['PARes'] = pares
        self.postString = urlencode(self.packet)
        self.url = self.callback
        self.valid = True
        
    def process(self):
        # Execute the post to protx VSP DIRECT        
        if self.valid:
            if self.settings.SKIP_POST.value:
                log.info("TESTING MODE - Skipping post to server.  Would have posted %s?%s", self.url, self.postString)
                record_payment(self.order, self.settings, amount=self.order.balance, transaction_id="TESTING")
                return (True, 'OK', "TESTING MODE")

            else:
                self.log_extra("About to post to server: %s?%s", self.url, self.postString)
                conn = urllib2.Request(self.url, data=self.postString)
                try:
                    f = urllib2.urlopen(conn)
                    result = f.read()
                    self.log_extra('Process: url=%s\nPacket=%s\nResult=%s', self.url, self.packet, result)
                except urllib2.URLError, ue:
                    log.error("error opening %s\n%s", self.url, ue)
                    print ue
                    return (False, 'ERROR', 'Could not talk to Protx gateway')
                try:
                    self.response = dict([row.split('=', 1) for row in result.splitlines()])
                    status = self.response['Status']
                    success = (status == 'OK')
                    detail = self.response['StatusDetail']
                    if success:
                        log.info('Success on order #%i, recording payment', self.order.id)
                        record_payment(self.order, self.settings, amount=self.order.balance) #, transaction_id=transaction_id)
                    return (success, status, detail)
                except Exception, e:
                    log.info('Error submitting payment: %s', e)
                    return (False, 'ERROR', 'Invalid response from payment gateway')
        else:
            return (False, 'ERROR', 'Error processing payment.')
