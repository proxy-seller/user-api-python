import requests
import json


# Powered by GPT4 - muhaha
class Api:
    URL = 'https://proxy-seller.com/personal/api/v1/'
    paymentId = 1
    generateAuth = 'N'

    def __init__(self, config):
        """
        API key placed in https://proxy-seller.com/personal/api/.

        Args:
            config (dict): Configuration options.

        Raises:
            Exception: If an error occurs during the configuration process.
        """

        if 'key' not in config:
            raise ValueError(
                "Need key, placed in https://proxy-seller.com/personal/api/")
        self.base_uri = self.URL + config['key'] + "/"
        self.session = requests.Session()

    def setPaymentId(self, id):
        self.paymentId = id

    def getPaymentId(self):
        return self.paymentId

    def setGenerateAuth(self, yn):
        if yn == 'Y':
            self.generateAuth = 'Y'
        else:
            self.generateAuth = 'N'

    def getGenerateAuth(self):
        return self.generateAuth

    def request(self, method, uri, **options):
        """
        Send a request to the server.

        Args:
            method (str): The HTTP method to use for the request.
            uri (str): The URI to send the request to.
            options (dict): Additional options for the request.

        Returns:
            mixed: The response from the server.

        Raises:
            Exception: If an error occurs during the request.
        """
        if options is None:
            options = {}

        response = self.session.request(method, self.base_uri + uri, **options)
        try:
            data = json.loads(response.text)
            if 'status' in data and data['status'] == 'success':  # Normal response
                return data['data']
            elif 'errors' in data:  # Normal error response
                raise ValueError(data['errors'][0]['message'])
            else:  # raw data
                return str(response.content)
        except json.decoder.JSONDecodeError:
            return response.content

    def authList(self):
        """
        Get auths

        Returns:
            array list auths
        """
        return self.request('GET', 'auth/list')

    def authActive(self, id, active):
        """
        Set auth active state

        Args:
            id (int): auth id
            active (str): active state (Y/N)

        Returns:
            array list auths
        """
        return self.request('POST', 'auth/active', json={'id': id, 'active': active})

    def balance(self):
        """
        Retrieve the balance statistic.

        Returns:
            float: The balance statistic.
        """
        return self.request('GET', 'balance/get')['summ']

    def balanceAdd(self, summ=5, paymentId=29):
        """
        Replenish the balance.

        Args:
            summ (float): The amount to add to the balance.
            payment_id (int): The identifier for the payment method.

        Returns:
            str: A link to the payment page. An example is shown below.
                'https://proxy-seller.com/personal/pay/?ORDER_ID=123456789&PAYMENT_ID=987654321&HASH=343bd596fb97c04bfb76557710837d34'
        """
        return self.request('POST', 'balance/add', json={'summ': summ, 'paymentId': paymentId})['url']

    def balancePaymentsList(self):
        """
        Retrieve a list of payment systems for balance replenishing.

        Returns:
            list: An example of the returned value is shown below.
                [
                    {
                        'id': '29',
                        'name': 'PayPal'
                    },
                    {
                        'id': '37',
                        'name': 'Visa / MasterCard'
                    }
                ]
        """
        return self.request('GET', 'balance/payments/list')['items']

    def referenceList(self, type=None):
        """
        Retrieve necessary guides for creating an order.
        This includes: 
        - Countries, operators and rotation periods (mobile only)
        - Proxy periods
        - Purposes and services (only for ipv4, ipv6, isp, mix)
        - Allowed quantities (only for mix proxy)

        Args:
            type (str): The type of the proxy - ipv4, ipv6, mobile, isp, mix, or null.

        Returns:
            dict: The guide information for creating an order.
        """
        return self.request('GET', 'reference/list/' + str(type))

    def prepare(self, **kwargs):
        allLocals = dict(kwargs)
        return allLocals

    def orderCalc(self, data):
        """
        Calculate the order.

        Args:
            json (dict): Free format dictionary to send into endpoint.

        Returns:
            dict: The response from the endpoint.
        """
        return self.request('POST', 'order/calc', json=data)

    def orderCalcIpv4(self, countryId, periodId, quantity, authorization, coupon, customTargetName):
        """
        Calculate the preliminary order for IPv4.
        Preliminary order calculation.
        An error in the warning must be corrected before placing an order.

        Args:
            country_id (int): The identifier for the country.
            period_id (int): The identifier for the period.
            quantity (int): The quantity of items.
            authorization (str): IP address.
            coupon (str): The coupon code.
            custom_target_name (str): The custom name for the target.

        Returns:
            dict: An example of the returned value is shown below.
                {
                    'warning': 'Insufficient funds. Total $2. Not enough $33.10',
                    'balance': 2,
                    'total': 35.1,
                    'quantity': 5,
                    'currency': 'USD',
                    'discount': 0.22,
                    'price': 7.02
                }
        """
        return self.orderCalc(
            self.prepare(paymentId=self.paymentId, generateAuth=self.generateAuth, countryId=countryId,
                         periodId=periodId, quantity=quantity, authorization=authorization, coupon=coupon,
                         customTargetName=customTargetName))

    def orderCalcIsp(self, countryId, periodId, quantity, authorization, coupon, customTargetName):
        """
        Calculate the preliminary order for ISP.
        Preliminary order calculation.
        An error in the warning must be corrected before placing an order.

        Args:
            country_id (int): The identifier for the country.
            period_id (int): The identifier for the period.
            quantity (int): The quantity of items.
            authorization (str): IP address.
            coupon (str): The coupon code.
            custom_target_name (str): The custom name for the target.

        Returns:
            dict: An example of the returned value is shown below.
                {
                    'warning': 'Insufficient funds. Total $2. Not enough $33.10',
                    'balance': 2,
                    'total': 35.1,
                    'quantity': 5,
                    'currency': 'USD',
                    'discount': 0.22,
                    'price': 7.02
                }
        """
        return self.orderCalc(
            self.prepare(paymentId=self.paymentId, generateAuth=self.generateAuth, countryId=countryId,
                         periodId=periodId, quantity=quantity, authorization=authorization, coupon=coupon,
                         customTargetName=customTargetName))

    def orderCalcMix(self, countryId, periodId, quantity, authorization, coupon, customTargetName):
        """
        Calculate the preliminary order for MIX.
        Preliminary order calculation.
        An error in the warning must be corrected before placing an order.

        Args:
            country_id (int): The identifier for the country.
            period_id (int): The identifier for the period.
            quantity (int): The quantity of items.
            authorization (str): IP address.
            coupon (str): The coupon code.
            custom_target_name (str): The custom name for the target.

        Returns:
            dict: An example of the returned value is shown below.
                {
                    'warning': 'Insufficient funds. Total $2. Not enough $33.10',
                    'balance': 2,
                    'total': 35.1,
                    'quantity': 5,
                    'currency': 'USD',
                    'discount': 0.22,
                    'price': 7.02
                }
        """
        return self.orderCalc(
            self.prepare(paymentId=self.paymentId, generateAuth=self.generateAuth, countryId=countryId,
                         periodId=periodId, quantity=quantity, authorization=authorization, coupon=coupon,
                         customTargetName=customTargetName))

    def orderCalcIpv6(self, countryId, periodId, quantity, authorization, coupon, customTargetName, protocol):
        """
        Calculate the preliminary order for IPv6.
        Preliminary order calculation.
        An error in the warning must be corrected before placing an order.

        Args:
            country_id (int): The identifier for the country.
            period_id (int): The identifier for the period.
            quantity (int): The quantity of items.
            authorization (str): IP address.
            coupon (str): The coupon code.
            custom_target_name (str): The custom name for the target.
            protocol (str): The protocol HTTPS or SOCKS5.

        Returns:
            dict: An example of the returned value is shown below.
                {
                    'warning': 'Insufficient funds. Total $2. Not enough $33.10',
                    'balance': 2,
                    'total': 35.1,
                    'quantity': 5,
                    'currency': 'USD',
                    'discount': 0.22,
                    'price': 7.02
                }
        """
        return self.orderCalc(
            self.prepare(paymentId=self.paymentId, generateAuth=self.generateAuth, countryId=countryId,
                         periodId=periodId, quantity=quantity, authorization=authorization, coupon=coupon,
                         customTargetName=customTargetName, protocol=protocol))

    def orderCalcMobile(self, countryId, periodId, quantity, authorization, coupon, operatorId, rotationId):
        """
        Calculate the preliminary order for Mobile.
        Preliminary order calculation.
        An error in the warning must be corrected before placing an order.

        Args:
            country_id (int): The identifier for the country.
            period_id (int): The identifier for the period.
            quantity (int): The quantity of items.
            authorization (str): IP address.
            coupon (str): The coupon code.
            operatorId (int): The identifier for the operator.
            rotationId (int): The identifier for the rotation.

        Returns:
            dict: An example of the returned value is shown below.
                {
                    'warning': 'Insufficient funds. Total $2. Not enough $33.10',
                    'balance': 2,
                    'total': 35.1,
                    'quantity': 5,
                    'currency': 'USD',
                    'discount': 0.22,
                    'price': 7.02
                }
        """
        return self.orderCalc(
            self.prepare(paymentId=self.paymentId, generateAuth=self.generateAuth, countryId=countryId,
                         periodId=periodId, quantity=quantity, authorization=authorization, coupon=coupon,
                         operatorId=operatorId, rotationId=rotationId))

    def orderCalcResident(self, tarifId, coupon):
        """
        Calculate the preliminary order for Resident.
        Preliminary order calculation.
        An error in the warning must be corrected before placing an order.

        Args:
            tarifId (int): The identifier for the tarif.
            coupon (str): The coupon code.

        Returns:
            dict: An example of the returned value is shown below.
                {
                    'orderId': 1000000,
                    'total': 35.1,
                    'balance': 10.19
                }
        """
        return self.orderCalc(
            self.prepare(paymentId=self.paymentId, tarifId=tarifId, coupon=coupon))

    def orderMakeIpv4(self, countryId, periodId, quantity, authorization, coupon, customTargetName):
        """
        Create an order for IPv4.

        Attention! Calling this method will deduct funds from your balance!
        The parameters are identical to the /order/calc method. Practice there before calling the /order/make method.

        Args:
            country_id (int): The identifier for the country.
            period_id (int): The identifier for the period.
            quantity (int): The quantity of items.
            authorization (str): The authorization token.
            coupon (str): The coupon code.
            custom_target_name (str): The custom name for the target.

        Returns:
            dict: An example of the returned value is shown below.
                {
                    'orderId': 1000000,
                    'total': 35.1,
                    'balance': 10.19
                }
        """
        return self.orderMake(
            self.prepare(paymentId=self.paymentId, generateAuth=self.generateAuth, countryId=countryId,
                         periodId=periodId, quantity=quantity, authorization=authorization, coupon=coupon,
                         customTargetName=customTargetName))

    def orderMakeIsp(self, countryId, periodId, quantity, authorization, coupon, customTargetName):
        """
        Create an order for ISP.

        Attention! Calling this method will deduct funds from your balance!
        The parameters are identical to the /order/calc method. Practice there before calling the /order/make method.

        Args:
            country_id (int): The identifier for the country.
            period_id (int): The identifier for the period.
            quantity (int): The quantity of items.
            authorization (str): The authorization token.
            coupon (str): The coupon code.
            custom_target_name (str): The custom name for the target.

        Returns:
            dict: An example of the returned value is shown below.
                {
                    'orderId': 1000000,
                    'total': 35.1,
                    'balance': 10.19
                }
        """
        return self.orderMake(
            self.prepare(paymentId=self.paymentId, generateAuth=self.generateAuth, countryId=countryId,
                         periodId=periodId, quantity=quantity, authorization=authorization, coupon=coupon,
                         customTargetName=customTargetName))

    def orderMakeMix(self, countryId, periodId, quantity, authorization, coupon, customTargetName):
        """
        Create an order for MIX.

        Attention! Calling this method will deduct funds from your balance!
        The parameters are identical to the /order/calc method. Practice there before calling the /order/make method.

        Args:
            country_id (int): The identifier for the country.
            period_id (int): The identifier for the period.
            quantity (int): The quantity of items.
            authorization (str): The authorization token.
            coupon (str): The coupon code.
            custom_target_name (str): The custom name for the target.

        Returns:
            dict: An example of the returned value is shown below.
                {
                    'orderId': 1000000,
                    'total': 35.1,
                    'balance': 10.19
                }
        """
        return self.orderMake(
            self.prepare(paymentId=self.paymentId, generateAuth=self.generateAuth, countryId=countryId,
                         periodId=periodId, quantity=quantity, authorization=authorization, coupon=coupon,
                         customTargetName=customTargetName))

    def orderMakeIpv6(self, countryId, periodId, quantity, authorization, coupon, customTargetName, protocol):
        """
        Create an order for IPv6.

        Attention! Calling this method will deduct funds from your balance!
        The parameters are identical to the /order/calc method. Practice there before calling the /order/make method.

        Args:
            country_id (int): The identifier for the country.
            period_id (int): The identifier for the period.
            quantity (int): The quantity of items.
            authorization (str): The authorization token.
            coupon (str): The coupon code.
            custom_target_name (str): The custom name for the target.
            protocol (str): The protocol HTTPS or SOCKS5.

        Returns:
            dict: An example of the returned value is shown below.
                {
                    'orderId': 1000000,
                    'total': 35.1,
                    'balance': 10.19
                }
        """
        return self.orderMake(
            self.prepare(paymentId=self.paymentId, generateAuth=self.generateAuth, countryId=countryId,
                         periodId=periodId, quantity=quantity, authorization=authorization, coupon=coupon,
                         customTargetName=customTargetName, protocol=protocol))

    def orderMakeMobile(self, countryId, periodId, quantity, authorization, coupon, operatorId, rotationId):
        """
        Create an order for Mobile.

        Attention! Calling this method will deduct funds from your balance!
        The parameters are identical to the /order/calc method. Practice there before calling the /order/make method.

        Args:
            country_id (int): The identifier for the country.
            period_id (int): The identifier for the period.
            quantity (int): The quantity of items.
            authorization (str): The authorization token.
            coupon (str): The coupon code.
            operatorId (int): The identifier for the operator.
            rotationId (int): The identifier for the rotation.

        Returns:
            dict: An example of the returned value is shown below.
                {
                    'orderId': 1000000,
                    'total': 35.1,
                    'balance': 10.19
                }
        """
        return self.orderMake(
            self.prepare(paymentId=self.paymentId, generateAuth=self.generateAuth, countryId=countryId,
                         periodId=periodId, quantity=quantity, authorization=authorization, coupon=coupon,
                         operatorId=operatorId, rotationId=rotationId))

    def orderMakeResident(self, tarifId, coupon):
        """
        Create an order for Resident.

        Attention! Calling this method will deduct funds from your balance!
        The parameters are identical to the /order/calc method. Practice there before calling the /order/make method.

        Args:
            tarifId (int): The identifier for the tarif.
            coupon (str): The coupon code.

        Returns:
            dict: An example of the returned value is shown below.
                {
                    'orderId': 1000000,
                    'total': 35.1,
                    'balance': 10.19
                }
        """
        return self.orderMake(
            self.prepare(paymentId=self.paymentId, tarifId=tarifId, coupon=coupon))

    def orderMake(self, data):
        """
        Create an order.

        Args:
            json (dict): Free format dictionary to send into endpoint.

        Returns:
            dict: The response from the endpoint.
        """
        return self.request('POST', 'order/make', json=data)

    def prolongCalc(self, type, ids, periodId, coupon=''):
        """
        Calculate the renewal.

        Args:
            type (str): The type of the order - ipv4, ipv6, mobile, isp, or mix.
            ids (list): A list of identifiers proxy.
            period_id (str): The identifier for the period.
            coupon (str): The coupon code.

        Returns:
            dict: An example of the returned value is shown below.
                {
                    'warning': 'Insufficient funds. Total $2. Not enough $33.10',
                    'balance': 2,
                    'total': 35.1,
                    'quantity': 5,
                    'currency': 'USD',
                    'discount': 0.22,
                    'price': 7.02,
                    'items': [],
                    'orders': 1
                }
        """
        return self.request('POST', 'prolong/calc/' + type, json={'ids': ids, 'periodId': periodId, 'coupon': coupon})

    def prolongMake(self, type, ids, periodId, coupon=''):
        """
        Create a renewal order.

        Attention! Calling this method will deduct $ from your balance!
        The parameters are identical to the /prolong/calc method. Practice there before calling the /prolong/make method.

        Args:
            type (str): The type of the order - ipv4, ipv6, mobile, isp, or mix.
            ids (list): A list of identifiers proxy.
            period_id (str): The identifier for the period.
            coupon (str): The coupon code.

        Returns:
            dict: An example of the returned value is shown below.
                {
                    'orderId': 1000000,
                    'total': 35.1,
                    'balance': 10.19
                }
        """
        return self.request('POST', 'prolong/make/' + type, json={'ids': ids, 'periodId': periodId, 'coupon': coupon})

    def proxyList(self, type=None):
        """
        Retrieve the list of proxies.

        Args:
            type (str): The type of the proxy - ipv4, ipv6, mobile, isp, mix, or null.

        Returns:
            dict: An example of the returned value is shown below.
                {
                    'id': 9876543,
                    'order_id': 123456,
                    'basket_id': 9123456,
                    'ip': '127.0.0.2',
                    'ip_only': '127.0.0.2',
                    'protocol': 'HTTP',
                    'port_socks': 50101,
                    'port_http': 50100,
                    'login': 'login',
                    'password': 'password',
                    'auth_ip': '',
                    'rotation': '',
                    'link_reboot': '#',
                    'country': 'France',
                    'country_alpha3': 'FRA',
                    'status': 'Active',
                    'status_type': 'ACTIVE',
                    'can_prolong': 1,
                    'date_start': '26.06.2023',
                    'date_end': '26.07.2023',
                    'comment': '',
                    'auto_renew': 'Y',
                    'auto_renew_period': ''
                }
        """
        if type == None:
            return self.request('GET', 'proxy/list')
        return self.request('GET', 'proxy/list/' + str(type))

    def proxyDownload(self, type, ext=None, proto=None, listId=None):
        """
        Export a proxy of a certain type in txt or csv format.

        Args:
            type (str): The type of the proxy - ipv4 | ipv6 | mobile | isp | mix | resident.
            ext (str): txt | csv | None
            proto (str): https | socks5 | None
            listId (int): only for resident, if not set - will return ip from all sheets
        Returns:
            str: An example of the returned value is shown below.
                'login:password@127.0.0.2:50100'
        """
        return self.request('GET', 'proxy/download/' + type, params={'ext': ext, 'proto': proto, 'listId': listId})

    def proxyCommentSet(self, ids, comment=None):
        """
        Set a comment for a proxy.

        Args:
            ids (list): Any id, regardless of the type of proxy.
            comment (str): The comment to set.

        Returns:
            int: The number of proxies updated.
        """
        return self.request('POST', 'proxy/comment/set', json={'ids': ids, 'comment': comment})['updated']

    def proxyCheck(self, proxy):
        """
        Check a single proxy.

        Args:
            proxy (str): Available values - user:password@127.0.0.1:8080, user@127.0.0.1:8080, 127.0.0.1:8080.

        Returns:
            dict: An example of the returned value is shown below.
                {
                    'ip': '127.0.0.1',
                    'port': 8080,
                    'user': 'user',
                    'password': 'password',
                    'valid': True,
                    'protocol': 'HTTP',
                    'time': 1234
                }
        """
        return self.request('GET', 'tools/proxy/check', params={'proxy': proxy})

    def ping(self):
        """
        Check the availability of the service.

        Returns:
            float: A Unix timestamp representing the current time.
        """
        return self.request('GET', 'system/ping')['pong']


    def residentPackage(self):
        """
        Package Information
        Remaining traffic, end date

        Returns:
            dict: An example of the returned value is shown below.
                {
                    'is_active': true,
                    'rotation': 60,
                    'tarif_id': 2,
                    'traffic_limit': 7516192768,
                    'traffic_usage': 10,
                    'expired_at': "d.m.Y H:i:s",
                    'auto_renew': false
                }
        """
        return self.request('GET', 'resident/package')

    def residentGeo(self):
        """
        Database geo locations (zip ~300Kb, unzip ~3Mb)

        Returns:
            binary
        """
        return self.request('GET', 'resident/geo')

    def residentList(self):
        """
        List of existing ip list in a package
        You can download the list via endpoint /proxy/download/resident?listId=123

        Returns:
            array
        """
        return self.request('GET', 'resident/lists')

    def residentListRename(self, id, title):
        """
        Rename list in user package

        Args:
            id (int): List ID
            title (str): Title list

        Returns:
            int: The number of proxies updated.
        """
        return self.request('POST', 'resident/list/rename', json={'id': id, 'title': title})

    def residentListDelete(self, id):
        """
        Remove list from user package

        Args:
            id (int): List ID

        Returns:
            array: Updated list model
        """
        return self.request('DELETE', 'resident/list/delete', json={'id': id})
