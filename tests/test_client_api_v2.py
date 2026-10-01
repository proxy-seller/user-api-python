"""
Офлайн-проверки локальных гейтов SDK. Каждый ассерт повторяет поведение сервера
Client API v2, а не README.

Запуск: python -m unittest discover
"""

import json
import pickle
import socket
import traceback
import unittest

import requests

from proxy_seller_user_api import Api, ApiError

from .support import (NO_JSON, FakeClock, FakeResponse, FakeSession, envelope, error_envelope,
                      json_file, text_file)


def make_api(responses=None, **config):
    session = FakeSession(responses)
    clock = FakeClock()
    # Очередь запросов включена, как по умолчанию, но на фейковом времени: несколько
    # write/money подряд не ждут интервалы по-настоящему.
    options = {'key': 'API-KEY', 'session': session,
               'rate_limit': {'clock': clock.monotonic, 'sleep': clock.sleep}}
    options.update(config)
    return Api(options), session


class BaseUriTest(unittest.TestCase):
    def test_api_key_goes_into_path(self):
        api, _ = make_api()
        self.assertEqual(
            api.base_uri, 'https://proxy-seller.com/personal/api/v2/API-KEY/')

    def test_custom_base_url(self):
        api, _ = make_api(base_url='http://localhost:8080/personal/api/v2')
        self.assertEqual(api.base_uri, 'http://localhost:8080/personal/api/v2/API-KEY/')


class AssertTargetNameTest(unittest.TestCase):
    """Для ipv4/ipv6/isp цель обязательна; mix без цели проходит, если пакет распознан."""

    def test_ipv4_without_target_raises(self):
        with self.assertRaises(ValueError):
            Api._assert_target_name({'sectionCode': 'ipv4', 'countryId': 'C1', 'quantity': 2})

    def test_ipv4_with_target_passes(self):
        Api._assert_target_name(
            {'sectionCode': 'ipv4', 'countryId': 'C1', 'customTargetName': 'seo'})

    def test_resident_section_is_not_gated(self):
        Api._assert_target_name({'sectionCode': 'resident', 'tarifId': 'T1'})

    def test_mix_by_mix_id_passes(self):
        Api._assert_target_name({'sectionCode': 'mix', 'mixId': 'M1'})

    def test_mix_by_mix_code_passes(self):
        Api._assert_target_name({'sectionCode': 'mix', 'mixCode': 'mix-us'})

    def test_mix_by_country_id_with_colon_passes(self):
        # countryId = "packageId:quantity" — вторая форма выбора mix-пакета
        Api._assert_target_name({'sectionCode': 'mix', 'countryId': 'PKG:5'})

    def test_mix_by_country_id_and_quantity_passes(self):
        # третья форма: countryId = packageId, quantity отдельно
        Api._assert_target_name({'sectionCode': 'mix', 'countryId': 'PKG', 'quantity': 5})

    def test_mix_by_country_id_and_string_quantity_passes(self):
        Api._assert_target_name({'sectionCode': 'mix', 'countryId': 'PKG', 'quantity': '5'})

    def test_mix_isp_by_country_id_and_quantity_passes(self):
        Api._assert_target_name({'sectionCode': 'mix_isp', 'countryId': 'PKG', 'quantity': 3})

    def test_mix_without_selection_requires_target(self):
        with self.assertRaises(ValueError):
            Api._assert_target_name({'sectionCode': 'mix', 'periodId': 'P1'})

    def test_mix_with_zero_quantity_requires_target(self):
        with self.assertRaises(ValueError):
            Api._assert_target_name({'sectionCode': 'mix', 'countryId': 'PKG', 'quantity': 0})

    def test_mix_isp_without_selection_requires_target(self):
        with self.assertRaises(ValueError):
            Api._assert_target_name({'sectionCode': 'mix_isp'})

    def test_order_calc_runs_the_gate(self):
        api, session = make_api()
        with self.assertRaises(ValueError):
            api.orderCalc({'sectionCode': 'isp', 'countryId': 'C1'})
        self.assertEqual(session.calls, [])


class DeleteResultTest(unittest.TestCase):
    """data delete-эндпоинтов приходит строкой ("delete" или JSON внутри строки)."""

    def test_plain_string(self):
        self.assertEqual(Api._delete_result('delete'), {'status': 'delete'})

    def test_json_string(self):
        self.assertEqual(Api._delete_result('{"status": "not-found"}'), {'status': 'not-found'})

    def test_dict_passthrough(self):
        self.assertEqual(Api._delete_result({'status': 'delete'}), {'status': 'delete'})

    def test_none(self):
        self.assertEqual(Api._delete_result(None), {})

    def test_resident_list_delete_normalizes(self):
        api, _ = make_api([envelope('delete')])
        self.assertEqual(api.residentListDelete(561), {'status': 'delete'})

    def test_subuser_list_delete_reports_not_found(self):
        api, _ = make_api([envelope(json.dumps({'status': 'not-found'}))])
        self.assertEqual(
            api.residentSubUserListDelete('KEY', 123), {'status': 'not-found'})


class ProxyDownloadTest(unittest.TestCase):
    def test_resident_with_package_key_raises(self):
        api, session = make_api()
        with self.assertRaises(ValueError):
            api.proxyDownload('resident', package_key='PKG')
        self.assertEqual(session.calls, [])

    def test_subresident_with_package_key_is_sent(self):
        api, session = make_api([text_file('1.2.3.4:8080')])
        self.assertEqual(api.proxyDownload('subresident', ext='txt', package_key='PKG'),
                         '1.2.3.4:8080')
        call = session.last
        self.assertTrue(call['url'].endswith('proxy/download/subresident'))
        self.assertEqual(call['params'], {'ext': 'txt', 'package_key': 'PKG'})

    def test_resident_without_package_key_is_allowed(self):
        api, session = make_api([text_file('1.2.3.4:8080')])
        api.proxyDownload('resident', ext='txt')
        self.assertEqual(session.last['params'], {'ext': 'txt'})

    def test_ext_validation(self):
        with self.assertRaises(ValueError):
            Api.assertExt('a' * 251)
        for bad in ('a\r', 'a\n', 'a/b', 'a\\b'):
            with self.assertRaises(ValueError):
                Api.assertExt(bad)
        self.assertEqual(Api.assertExt('%ip%:%port%'), '%ip%:%port%')

    def test_geo_is_returned_as_json_file_bytes(self):
        api, _ = make_api([json_file('[{"code": "US"}]')])
        raw = api.residentGeo()
        self.assertIsInstance(raw, bytes)
        self.assertEqual(json.loads(raw.decode('utf-8')), [{'code': 'US'}])


class AutoTopupTest(unittest.TestCase):
    STATE = {
        'configured': True, 'enabled': True, 'state': 'ACTIVE',
        'threshold': 10, 'amount': 25, 'subscriptionId': 'sub_1',
        'paymentMethod': {'id': 'sub_1', 'status': 'active', 'paymentMethod': 'card',
                          'brand': 'visa', 'last4': '4242', 'exp': '01/2030'},
        'failCount': 0,
        'lastAttemptAt': None, 'lastEvent': None,
    }

    def test_get_uses_get_endpoint(self):
        api, session = make_api([envelope(self.STATE)])
        self.assertEqual(api.balanceAutoTopupGet(), self.STATE)
        self.assertEqual(session.last['method'], 'GET')
        self.assertTrue(session.last['url'].endswith('balance/autotopup/get'))

    def test_partial_update_sends_only_given_field(self):
        api, session = make_api([envelope(self.STATE)])
        api.balanceAutoTopupSet(threshold=20)
        self.assertEqual(session.last['method'], 'POST')
        self.assertTrue(session.last['url'].endswith('balance/autotopup/set'))
        self.assertEqual(session.last['json'], {'threshold': 20})

    def test_false_and_zero_are_not_dropped(self):
        api, session = make_api([envelope(self.STATE)])
        api.balanceAutoTopupSet(enabled=False, threshold=0)
        self.assertEqual(session.last['json'], {'enabled': False, 'threshold': 0})

    def test_full_payload(self):
        api, session = make_api([envelope(self.STATE)])
        api.balanceAutoTopupSet(
            enabled=True, threshold=10, amount=25, subscriptionId='sub_1')
        self.assertEqual(session.last['json'], {
            'enabled': True, 'threshold': 10, 'amount': 25, 'subscriptionId': 'sub_1'})

    def test_removed_caps_are_rejected_locally(self):
        """Сервер эти поля молча игнорирует (убраны 18.08.2026), так что отправить их —
        значит получить success, не изменивший ничего. Отбиваем до запроса."""
        api, session = make_api([envelope(self.STATE)])
        for field in ('dailyCountCap', 'monthlyAmountCap'):
            with self.assertRaises(ValueError):
                api.balanceAutoTopupSet(**{field: 3})
        self.assertEqual(session.calls, [])

    def test_dict_form_drops_none(self):
        api, session = make_api([envelope(self.STATE)])
        api.balanceAutoTopupSet({'enabled': True, 'threshold': None, 'amount': 25})
        self.assertEqual(session.last['json'], {'enabled': True, 'amount': 25})

    def test_omitted_fields_never_leave_as_null(self):
        api, session = make_api([envelope(self.STATE)])
        api.balanceAutoTopupSet(amount=25)
        self.assertNotIn('enabled', session.last['json'])
        self.assertNotIn('threshold', session.last['json'])
        self.assertNotIn('subscriptionId', session.last['json'])

    def test_validation_limits_are_exposed(self):
        api, _ = make_api([error_envelope([
            {'message': 'Top-up amount must be 5 or more', 'code': 51,
             'customData': {'minAmount': 5, 'minThreshold': 1, 'minDailyCountCap': 1}}])])
        with self.assertRaises(ApiError) as ctx:
            api.balanceAutoTopupSet(amount=1)
        self.assertEqual(ctx.exception.code, 51)
        self.assertEqual(ctx.exception.custom_data['minAmount'], 5)
        self.assertFalse(ctx.exception.isAccessError())

    def test_feature_disabled_code(self):
        api, _ = make_api([error_envelope(
            [{'message': 'Auto top-up is not available', 'code': 49}])])
        with self.assertRaises(ApiError) as ctx:
            api.balanceAutoTopupGet()
        self.assertEqual(ctx.exception.code, 49)


class BalanceAddTest(unittest.TestCase):
    def test_payment_code_only_raises_locally(self):
        api, session = make_api()
        api.setPaymentCode('balance')
        with self.assertRaises(ValueError) as ctx:
            api.balanceAdd(10)
        self.assertIn('paymentCode', str(ctx.exception))
        self.assertEqual(session.calls, [])

    def test_explicit_payment_code_argument_raises(self):
        api, session = make_api()
        with self.assertRaises(ValueError):
            api.balanceAdd(10, paymentId='PS1', paymentCode='balance')
        self.assertEqual(session.calls, [])

    def test_payment_id_is_sent(self):
        api, session = make_api([envelope({'url': 'https://pay/1'})])
        self.assertEqual(api.balanceAdd(10, 'PS1'), 'https://pay/1')
        self.assertEqual(session.last['json'], {'summ': 10, 'paymentId': 'PS1'})

    def test_payment_id_from_setter(self):
        api, session = make_api([envelope({'url': 'https://pay/2'})])
        api.setPaymentId('PS2')
        api.balanceAdd(5)
        self.assertEqual(session.last['json'], {'summ': 5, 'paymentId': 'PS2'})


class ProxyReplaceTest(unittest.TestCase):
    def test_reason_enum_is_checked(self):
        api, session = make_api()
        with self.assertRaises(ValueError):
            api.proxyReplace(['ID1'], 'ipv4')
        self.assertEqual(session.calls, [])

    def test_missing_reason_raises(self):
        api, _ = make_api()
        with self.assertRaises(ValueError):
            api.proxyReplace(['ID1'])

    def test_custom_requires_comment(self):
        api, _ = make_api()
        with self.assertRaises(ValueError):
            api.proxyReplace(['ID1'], 'CUSTOM')
        with self.assertRaises(ValueError):
            api.proxyReplace(['ID1'], 'CUSTOM', '   ')

    def test_custom_with_comment_passes(self):
        api, session = make_api([envelope({'items': []})])
        api.proxyReplace(['ID1'], 'custom', 'ports are blocked')
        self.assertEqual(session.last['json'], {
            'ids': ['ID1'], 'type': 'CUSTOM', 'comment': 'ports are blocked'})

    def test_reason_alias(self):
        api, session = make_api([envelope({'items': []})])
        api.proxyReplace(['ID1'], reason='low_speed')
        self.assertEqual(session.last['json']['type'], 'LOW_SPEED')

    def test_all_enum_values_accepted(self):
        for value in Api.PROXY_REPLACE_TYPES:
            api, session = make_api([envelope({'items': []})])
            api.proxyReplace(['ID1'], value, 'comment')
            self.assertEqual(session.last['json']['type'], value)


class ResidentGeoPayloadTest(unittest.TestCase):
    def test_empty_geo_list_is_coerced_to_object(self):
        api, session = make_api([envelope({'id': 1})])
        api.residentListAdd({'title': 'L', 'geo': []})
        self.assertEqual(session.last['json']['geo'], {})

    def test_single_element_geo_list_is_unwrapped(self):
        api, session = make_api([envelope({'id': 1})])
        api.residentListAdd({'title': 'L', 'geo': [{'country': 'US'}]})
        self.assertEqual(session.last['json']['geo'], {'country': 'US'})

    def test_wrong_geo_type_raises_value_error(self):
        api, session = make_api()
        with self.assertRaises(ValueError):
            api.residentListAdd({'title': 'L', 'geo': 'US'})
        with self.assertRaises(ValueError):
            api.residentListAdd({'title': 'L', 'geo': [{'country': 'US'}, {'country': 'DE'}]})
        self.assertEqual(session.calls, [])

    def test_positional_form_builds_geo_object(self):
        api, session = make_api([envelope({'id': 1})])
        api.residentListAdd('L', country='US', region='Washington', rotation=60)
        self.assertEqual(session.last['json']['geo'], {'country': 'US', 'region': 'Washington'})
        self.assertEqual(session.last['json']['rotation'], 60)

    def test_subuser_list_add_geo(self):
        api, session = make_api([envelope({'id': 1})])
        api.residentSubUserListAdd('KEY', title='L', geo={'country': 'US', 'city': None})
        self.assertEqual(session.last['json']['geo'], {'country': 'US'})

    def test_subuser_list_add_rejects_bad_geo(self):
        api, session = make_api()
        with self.assertRaises(ValueError):
            api.residentSubUserListAdd('KEY', geo='US')
        self.assertEqual(session.calls, [])


class ErrorEnvelopeTest(unittest.TestCase):
    ACCESS_TRIPLE = [
        {'message': 'Error api key', 'code': 503},
        {'message': 'IP not allowed 1.2.3.4', 'code': 503},
        {'message': 'Request limit reached', 'code': 503},
    ]

    def test_whole_errors_array_is_available(self):
        api, _ = make_api([error_envelope(self.ACCESS_TRIPLE)])
        with self.assertRaises(ApiError) as ctx:
            api.balance()
        error = ctx.exception
        self.assertEqual(str(error), 'Error api key')
        self.assertEqual(len(error.errors), 3)
        self.assertEqual(error.errors[2]['message'], 'Request limit reached')
        self.assertEqual(error.http_status, 200)
        self.assertTrue(error.isAccessError())

    def test_business_error_is_not_access_error(self):
        api, _ = make_api([error_envelope([{'message': 'Incorrect goal', 'code': 14}])])
        with self.assertRaises(ApiError) as ctx:
            api.orderMake({'sectionCode': 'resident', 'tarifId': 'T1'})
        self.assertFalse(ctx.exception.isAccessError())
        self.assertEqual(ctx.exception.errors, [{'message': 'Incorrect goal', 'code': 14}])

    def test_success_returns_data(self):
        api, _ = make_api([envelope({'summ': 12.34})])
        self.assertEqual(api.balance(), 12.34)

    def test_resident_lists_is_a_flat_array(self):
        api, _ = make_api([envelope([{'id': 1, 'title': 'L'}])])
        self.assertEqual(api.residentList(), [{'id': 1, 'title': 'L'}])


class FingerprintTest(unittest.TestCase):
    """
    X-Fingerprint необязателен для любой секции, включая resident и scraper: SDK не требует
    его локально и отправляет заголовок только тогда, когда значение задано.
    """

    def test_resident_and_scraper_orders_go_without_fingerprint(self):
        cases = (
            lambda api: api.orderMake({'sectionCode': 'resident', 'tarifId': '1-gb'}),
            lambda api: api.orderMake({'sectionCode': 'Scraper', 'tarifId': 'T1'}),
            lambda api: api.orderMakeResident('1-gb'),
        )
        for index, call in enumerate(cases):
            with self.subTest(call=index):
                api, session = make_api([envelope({'orderId': 'O1'})])
                self.assertEqual(call(api), {'orderId': 'O1'})
                self.assertTrue(session.last['url'].endswith('order/make'))
                self.assertNotIn('X-Fingerprint', session.last['headers'])

    def test_blank_fingerprint_is_not_sent(self):
        api, session = make_api([envelope({'orderId': 'O1'})], fingerprint='   ')
        api.orderMakeResident('1-gb')
        self.assertNotIn('X-Fingerprint', session.last['headers'])

    def test_configured_fingerprint_is_sent_for_any_section(self):
        api, session = make_api([envelope({'orderId': 'O1'}), envelope({'orderId': 'O2'})],
                                fingerprint='install-1')
        api.orderMakeResident('1-gb')
        self.assertEqual(session.last['headers']['X-Fingerprint'], 'install-1')
        api.orderMakeIpv4('USA', '1m', 1, customTargetName='seo')
        self.assertEqual(session.last['headers']['X-Fingerprint'], 'install-1')

    def test_per_call_fingerprint_wins_over_configured_one(self):
        api, session = make_api([envelope({'orderId': 'O1'}), envelope({'orderId': 'O2'})])
        api.setFingerprint('install-1')
        api.orderMake({'sectionCode': 'scraper', 'tarifId': 'T1'}, fingerprint='install-2')
        self.assertEqual(session.last['headers']['X-Fingerprint'], 'install-2')
        api.orderMakeResident('1-gb', fingerprint='install-3')
        self.assertEqual(session.last['headers']['X-Fingerprint'], 'install-3')


class SubPackageTest(unittest.TestCase):
    def test_traffic_limit_required(self):
        api, session = make_api()
        with self.assertRaises(ValueError):
            api.residentSubUserCreate(rotation=60)
        self.assertEqual(session.calls, [])

    def test_create_sends_expired_at_as_string(self):
        api, session = make_api([envelope({
            'package_key': 'KEY',
            'expired_at': {'date': '2026-12-31 00:00:00.000000',
                           'timezone_type': 3, 'timezone': 'UTC'}})])
        result = api.residentSubUserCreate(
            traffic_limit='1073741824', expired_at='2026-12-31 00:00:00')
        self.assertEqual(session.last['json']['expired_at'], '2026-12-31 00:00:00')
        # в ответе expired_at — объект PHP-даты, а не строка
        self.assertEqual(result['expired_at']['timezone_type'], 3)

    def test_traffic_details_requires_package_key_alias(self):
        api, session = make_api([envelope({'items': []})])
        api.residentTrafficDetails({'packageKey': 'KEY', 'date_start': '2026-08-01'})
        self.assertEqual(session.last['json'],
                         {'packageKey': 'KEY', 'date_start': '2026-08-01'})


class ProlongMakeInsufficientFundsTest(unittest.TestCase):
    """
    prolong/make при нехватке средств кладёт причину в errors[{code: 16}], поэтому её
    разбирает общий код конверта — отдельной обёртки в SDK больше нет.

    Раньше форма была другой (status="error" + ПУСТОЙ errors[]), и под неё существовал
    _assert_prolong_made. После смены формы его единственным оставшимся эффектом было
    превращать ЛЕГИТИМНЫЙ success с пустым orderId в фальшивую ошибку — уже ПОСЛЕ списания.
    """

    def test_insufficient_funds_raises_instead_of_looking_like_success(self):
        api, _ = make_api([error_envelope(
            [{'code': 16, 'message': 'Insufficient funds on balance'}])])
        with self.assertRaises(ApiError) as ctx:
            api.prolongMake('ipv4', ids=['68b1f0c4e13a4c0f1a2b3c4d'], periodId='1m')
        self.assertEqual(ctx.exception.code, 16)
        self.assertIn('Insufficient funds', str(ctx.exception))

    def test_success_with_empty_order_id_is_not_turned_into_an_error(self):
        """Деньги уже списаны — потерять total/balance здесь нельзя."""
        api, _ = make_api([envelope(
            {'orderId': '', 'total': 10, 'balance': 90, 'listBaseOrderNumbers': ['LH-1']})])
        result = api.prolongMake('ipv4', ids=['68b1f0c4e13a4c0f1a2b3c4d'], periodId='1m')
        self.assertEqual(result['total'], 10)
        self.assertEqual(result['listBaseOrderNumbers'], ['LH-1'])

    def test_successful_prolong_returns_every_renewed_order(self):
        """orderIds — все продлённые заказы, orderId — первый из них; SDK отдаёт оба как есть."""
        orders = ['6a248de4717805635cf6057d', '6a248de4717805635cf6058a']
        api, _ = make_api([envelope({
            'orderId': orders[0], 'orderIds': orders, 'total': 25,
            'listBaseOrderNumbers': ['NS_1790059585687-no', 'NS_1790059601234-kq'],
            'balance': 100.5})])
        result = api.prolongMake('ipv6', orders, '1m')
        self.assertEqual(result['orderIds'], orders)
        self.assertEqual(result['orderId'], result['orderIds'][0])
        self.assertEqual(len(result['listBaseOrderNumbers']), 2)

    def test_prolong_calc_warning_is_still_returned_not_raised(self):
        """У prolong/calc тот же конверт — ЛЕГИТИМНЫЙ warning, исключения быть не должно."""
        api, _ = make_api([envelope(
            {'warning': 'Insufficient funds. Total 3.0000. Not enough $7.00', 'total': 10},
            status='error')])
        result = api.prolongCalc('ipv4', ids=['68b1f0c4e13a4c0f1a2b3c4d'], periodId='1m')
        self.assertIn('Not enough', result['warning'])


class DocumentedOrderExamplesTest(unittest.TestCase):
    """
    Примеры из README: коды уезжают ПОЗИЦИОННО в *Id-аргументы, без цепочек None и без
    options ради кодов. У каждого *Id-поля есть серверный фолбэк: значение, не являющееся
    валидным id, резолвится как код, если соответствующий *Code пуст. Исключение —
    rotationId: он обязан быть ЧИСЛОМ МИНУТ (0 = "By Link"), '5m'/'10m' сервер отвергает.
    """

    def test_ipv4_example_passes_codes_positionally(self):
        api, session = make_api([envelope({'total': 10})])
        api.setPaymentCode('balance')
        api.orderCalcIpv4('USA', '1m', 2, customTargetName='seo', uptime=True)
        self.assertEqual(session.last['json'], {
            'sectionCode': 'ipv4', 'paymentCode': 'balance', 'countryId': 'USA',
            'periodId': '1m', 'quantity': 2, 'customTargetName': 'seo', 'uptime': True})

    def test_paying_for_orders_example_uses_the_two_order_payment_codes(self):
        """
        Заказ оплачивается только балансом или привязанной картой: в balancePaymentsList()
        самого баланса нет, поэтому платёжка заказа — фиксированный код, а не элемент списка.
        """
        for code in ('balance', 'paddle_subscription'):
            with self.subTest(code=code):
                api, session = make_api([envelope({'orderId': 'O1'})])
                api.setPaymentCode(code)
                api.orderMakeIpv4('USA', '1m', 1, customTargetName='seo')
                self.assertTrue(session.last['url'].endswith('order/make'))
                self.assertEqual(session.last['json']['paymentCode'], code)
                self.assertNotIn('paymentId', session.last['json'])

    def test_ipv4_example_without_target_would_fail_locally(self):
        """Головной пример README без customTargetName не доходил до сети."""
        api, session = make_api()
        with self.assertRaises(ValueError):
            api.orderCalcIpv4('USA', '1m', 2)
        self.assertEqual(session.calls, [])

    def test_mobile_example_sends_rotation_as_minutes(self):
        api, session = make_api([envelope({'total': 10})])
        api.orderCalcMobile('USA', '1m', 1, operatorId='ee_unitedkingdom', rotationId=5)
        payload = session.last['json']
        self.assertEqual(payload['countryId'], 'USA')
        self.assertEqual(payload['periodId'], '1m')
        self.assertEqual(payload['operatorId'], 'ee_unitedkingdom')
        self.assertEqual(payload['rotationId'], 5)
        self.assertIsInstance(payload['rotationId'], int)
        self.assertEqual(payload['mobileServiceType'], 'dedicated')
        # никаких *Code и никаких None в теле
        self.assertFalse([key for key in payload if key.endswith('Code')
                          and key != 'sectionCode'])
        self.assertNotIn(None, payload.values())

    def test_mix_example_passes_package_tag_positionally(self):
        api, session = make_api([envelope({'total': 10})])
        api.orderCalcMix('usa-europe-mix_IPv4', '1m', 1)
        self.assertEqual(session.last['json'], {
            'sectionCode': 'mix', 'mixId': 'usa-europe-mix_IPv4',
            'periodId': '1m', 'quantity': 1})

    def test_resident_example_passes_tarif_positionally(self):
        api, session = make_api([envelope({'total': 10})])
        api.orderCalcResident('TARIF_ID')
        self.assertEqual(session.last['json'], {
            'sectionCode': 'resident', 'tarifId': 'TARIF_ID'})

    def test_prolong_keyword_example_renews_whole_orders(self):
        """README: mix продлевается целым заказом — orderIds, коды — прямо в *Id-полях."""
        api, session = make_api([envelope({'orderId': '6a248de4717805635cf6057d'})])
        api.prolongMake('mix', orderIds=['6a248de4717805635cf6057d'], periodId='1m',
                        paymentId='balance', coupon='SALE10')
        self.assertTrue(session.last['url'].endswith('prolong/make/mix'))
        self.assertEqual(session.last['json'], {
            'orderIds': ['6a248de4717805635cf6057d'], 'periodId': '1m',
            'paymentId': 'balance', 'coupon': 'SALE10'})


class ProlongSelectionTest(unittest.TestCase):
    """
    "Что продлить" зависит от типа в пути: ipv4 / isp / mobile — отдельные прокси (ids — id
    из proxy/list, ips — адреса), ipv6 / mix / mix_isp — только целые заказы (orderIds —
    order_id). Второй аргумент (параметр ids) раскладывается по типу: у ipv4 / isp / mobile
    id прокси уходят в поле ids, у ipv6 / mix / mix_isp — в orderIds.
    """

    PROXY_ID = '68b1f0c4e13a4c0f1a2b3c4d'
    ORDER_ID = '6a248de4717805635cf6057d'

    def test_addresses_go_into_ips(self):
        api, session = make_api([envelope({'orderId': self.ORDER_ID})])
        api.prolongMake('ipv4', ['1.2.3.4', '5.6.7.8'], '1m')
        self.assertEqual(session.last['json'], {
            'ips': ['1.2.3.4', '5.6.7.8'], 'periodId': '1m', 'coupon': ''})

    def test_proxy_ids_go_into_ids_for_per_proxy_types(self):
        for proxy_type in ('ipv4', 'isp', 'mobile', 'IPv4', ' ISP '):
            with self.subTest(type=proxy_type):
                api, session = make_api([envelope({'total': 1})])
                api.prolongCalc(proxy_type, [self.PROXY_ID], '1m')
                self.assertEqual(session.last['json'], {
                    'ids': [self.PROXY_ID], 'periodId': '1m', 'coupon': ''})

    def test_order_ids_go_into_order_ids_for_whole_order_types(self):
        for proxy_type in ('ipv6', 'mix', 'mix_isp', 'IPv6', 'MIX-ISP', 'mix isp'):
            with self.subTest(type=proxy_type):
                api, session = make_api([envelope({'total': 1})])
                api.prolongCalc(proxy_type, [self.ORDER_ID], '1m')
                self.assertEqual(session.last['json'], {
                    'orderIds': [self.ORDER_ID], 'periodId': '1m', 'coupon': ''})

    def test_keyword_ids_is_routed_by_type(self):
        """
        ids= — это второй аргумент, а не проводное поле: он раскладывается по типу так же,
        как позиционный (у ipv4 / isp / mobile id прокси уходит в ids, у ipv6 / mix /
        mix_isp — в orderIds).
        """
        api, session = make_api([envelope({'total': 1}), envelope({'total': 1}),
                                 envelope({'orderId': self.ORDER_ID}),
                                 envelope({'autoProlong': False})])
        api.prolongCalc('ipv4', ids=[self.PROXY_ID], periodId='1m')
        self.assertEqual(session.last['json'], {
            'ids': [self.PROXY_ID], 'periodId': '1m', 'coupon': ''})
        api.prolongCalc('ipv6', ids=[self.ORDER_ID], periodId='1m')
        self.assertEqual(session.last['json']['orderIds'], [self.ORDER_ID])
        self.assertNotIn('ids', session.last['json'])
        api.prolongMake('mix', ids=[self.ORDER_ID], periodId='1m')
        self.assertEqual(session.last['json'], {
            'orderIds': [self.ORDER_ID], 'periodId': '1m', 'coupon': ''})
        api.autoProlongDisable('ipv6', ids=[self.ORDER_ID])
        self.assertEqual(session.last['json'], {'orderIds': [self.ORDER_ID]})

    def test_mobile_address_with_colons_is_an_address(self):
        """mobile: 'ip:port_http:port_socks' — двоеточия не мешают попасть в ips."""
        api, session = make_api([envelope({'total': 1})])
        api.prolongCalc('mobile', ['10.0.0.1:8000:9000'], '1m')
        self.assertEqual(session.last['json'], {
            'ips': ['10.0.0.1:8000:9000'], 'periodId': '1m', 'coupon': ''})

    def test_address_for_whole_order_type_is_left_for_the_server_to_reject(self):
        """
        ipv6 больше не продлевается по адресу 'host:port': адрес не переосмысляется в заказ,
        а уходит в ips, и сервер отвечает понятной ошибкой.
        """
        api, session = make_api([error_envelope([
            {'message': '[ips] is not applicable for ipv6: prolong by [orderIds]', 'code': 0}])])
        with self.assertRaises(ApiError) as ctx:
            api.prolongCalc('ipv6', ['1.2.3.4:26000'], '1m')
        self.assertIn('not applicable', str(ctx.exception))
        self.assertEqual(session.last['json'], {
            'ips': ['1.2.3.4:26000'], 'periodId': '1m', 'coupon': ''})

    def test_wrong_kind_of_explicit_field_is_left_for_the_server_to_reject(self):
        """
        Поля выбора в проводном виде уходят как есть: ids у ipv6 не переосмысляется в
        orderIds, orderIds у ipv4 — в ids. Сервер отвечает ошибкой с code 0, называющей
        нужное поле.
        """
        cases = (
            ('ipv6', {'ids': [self.ORDER_ID]},
             '[ids] is not applicable for ipv6: prolong by [orderIds]'),
            ('ipv4', {'orderIds': [self.ORDER_ID]},
             '[orderIds] is not applicable for ipv4: prolong by [ids]'),
        )
        for proxy_type, selection, message in cases:
            with self.subTest(type=proxy_type):
                api, session = make_api([error_envelope([{'message': message, 'code': 0}])])
                with self.assertRaises(ApiError) as ctx:
                    api.prolongCalc(proxy_type, None, '1m', options=selection)
                self.assertEqual(ctx.exception.code, 0)
                self.assertEqual(str(ctx.exception), message)
                self.assertEqual(session.last['json'], {
                    **selection, 'periodId': '1m', 'coupon': ''})

    def test_proxy_ids_and_addresses_are_not_combined(self):
        """
        Если пришли и ids, и ips, сервер продлевает по ids и игнорирует ips: смешанный список
        продлил бы только id, а адреса выпали бы молча, поэтому смесь отбивается до запроса —
        и позиционная, и явная.
        """
        api, session = make_api()
        with self.assertRaises(ValueError) as ctx:
            api.prolongCalc('ipv4', ['1.2.3.4', self.PROXY_ID], '1m')
        self.assertIn('renews by ids and ignores ips', str(ctx.exception))
        with self.assertRaises(ValueError):
            api.prolongMake('mobile', [self.PROXY_ID], '1m', ips=['10.0.0.1:8000:9000'])
        with self.assertRaises(ValueError):
            api.prolongMake('isp', None, '1m',
                            options={'ids': [self.PROXY_ID], 'ips': ['1.2.3.4']})
        self.assertEqual(session.calls, [])

    def test_mixed_list_for_whole_order_type_is_left_for_the_server(self):
        """У ipv6 / mix / mix_isp лишние ips сервер отбивает ошибкой, а не игнорирует."""
        api, session = make_api([envelope({'total': 1})])
        api.prolongCalc('mix', [self.ORDER_ID, '1.2.3.4'], '1m')
        self.assertEqual(session.last['json']['orderIds'], [self.ORDER_ID])
        self.assertEqual(session.last['json']['ips'], ['1.2.3.4'])

    def test_comma_string_and_blanks(self):
        api, session = make_api([envelope({'total': 1})])
        api.prolongCalc('ipv4', '1.2.3.4, 5.6.7.8 ,  ', '1m')
        self.assertEqual(session.last['json']['ips'], ['1.2.3.4', '5.6.7.8'])

    def test_empty_selection_is_not_sent(self):
        cases = (
            ([], {}),
            ('  , ', {}),
            (None, {'ips': (), 'orderIds': '', 'options': {'ids': []}}),
        )
        for ids, kwargs in cases:
            with self.subTest(ids=ids, kwargs=kwargs):
                api, session = make_api([envelope({'total': 1})])
                api.prolongCalc('ipv4', ids, '1m', **kwargs)
                self.assertEqual(session.last['json'], {'periodId': '1m', 'coupon': ''})

    def test_explicit_selection_field_is_sent_as_a_list(self):
        api, session = make_api([envelope({'total': 1})])
        api.prolongCalc('ipv6', orderIds=(self.ORDER_ID,), periodId='1m')
        self.assertEqual(session.last['json']['orderIds'], [self.ORDER_ID])

    def test_prepare_prolong_routes_by_type(self):
        """prepareProlong — публичный помощник: без type раскладывает как ipv4 / isp / mobile."""
        api, _ = make_api()
        self.assertEqual(api.prepareProlong([self.PROXY_ID], '1m'), {
            'ids': [self.PROXY_ID], 'periodId': '1m', 'coupon': ''})
        self.assertEqual(api.prepareProlong([self.ORDER_ID], '1m', type='mix_isp'), {
            'orderIds': [self.ORDER_ID], 'periodId': '1m', 'coupon': ''})


class ProlongRemovedFieldsTest(unittest.TestCase):
    """
    orderSeparatorIds и orderSeparatorId удалены из контракта prolong/* и autoprolong/*.
    Сервер их больше не читает, поэтому SDK не выбрасывает их молча, а отбивает до запроса
    с названием замены (orderIds) — из dict-формы, из options и из именованных аргументов.
    ids к удалённым не относится: это рабочее поле выбора ipv4 / isp / mobile.
    """

    PROXY_ID = '68b1f0c4e13a4c0f1a2b3c4d'
    ORDER_ID = '6a248de4717805635cf6057d'

    def assertRejectedBeforeRequest(self, call, *needles):
        api, session = make_api()
        with self.assertRaises(ValueError) as ctx:
            call(api)
        for needle in needles:
            self.assertIn(needle, str(ctx.exception))
        self.assertEqual(session.calls, [])

    def test_ids_key_is_sent_not_rejected(self):
        """ids в options, в dict-форме и в теле prepareProlong() уходит на провод как есть."""
        cases = (
            (lambda api: api.prolongCalc('ipv4', None, '1m', options={'ids': [self.PROXY_ID]}),
             'prolong/calc/ipv4', {'ids': [self.PROXY_ID], 'periodId': '1m', 'coupon': ''}),
            (lambda api: api.prolongMake('isp', {'ids': [self.PROXY_ID], 'periodId': '1m'}),
             'prolong/make/isp', {'ids': [self.PROXY_ID], 'periodId': '1m'}),
            (lambda api: api.autoProlongCalc('mobile', None, '1m', paymentId='balance',
                                             options={'ids': [self.PROXY_ID]}),
             'autoprolong/calc/mobile',
             {'ids': [self.PROXY_ID], 'periodId': '1m', 'paymentId': 'balance'}),
            (lambda api: api.autoProlongEnable('ipv4', {'ids': [self.PROXY_ID], 'periodId': '1m',
                                                        'paymentId': 'balance'}),
             'autoprolong/enable/ipv4',
             {'ids': [self.PROXY_ID], 'periodId': '1m', 'paymentId': 'balance'}),
            (lambda api: api.autoProlongDisable('isp', options={'ids': [self.PROXY_ID]}),
             'autoprolong/disable/isp', {'ids': [self.PROXY_ID]}),
        )
        for index, (call, path, body) in enumerate(cases):
            with self.subTest(call=index):
                api, session = make_api([envelope({'total': 1})])
                call(api)
                self.assertTrue(session.last['url'].endswith(path))
                self.assertEqual(session.last['json'], body)
        api, _ = make_api()
        self.assertEqual(api.prepareProlong(None, '1m', '', {'ids': [self.PROXY_ID]}), {
            'ids': [self.PROXY_ID], 'periodId': '1m', 'coupon': ''})

    def test_order_separator_keys_name_order_ids(self):
        calls = (
            lambda api: api.prolongMake('mix', None, '1m', orderSeparatorIds=['SEPARATOR_ID']),
            lambda api: api.prolongCalc('mix_isp', {'orderSeparatorId': 'SEPARATOR_ID',
                                                    'periodId': '1m'}),
            lambda api: api.autoProlongCalc('mix', None, '1m', paymentId='balance',
                                            orderSeparatorId='SEPARATOR_ID'),
            lambda api: api.autoProlongEnable('mix', None, '1m', paymentId='balance',
                                              options={'orderSeparatorIds': ['SEPARATOR_ID']}),
            lambda api: api.autoProlongDisable('mix_isp', orderSeparatorIds=['SEPARATOR_ID']),
        )
        for index, call in enumerate(calls):
            with self.subTest(call=index):
                self.assertRejectedBeforeRequest(call, 'removed: use orderIds')

    def test_every_removed_key_is_named(self):
        api, session = make_api()
        with self.assertRaises(ValueError) as ctx:
            api.prolongMake('mix', [self.ORDER_ID], '1m',
                            orderSeparatorIds=['SEPARATOR_ID'], orderSeparatorId='SEPARATOR_ID')
        self.assertIn('orderSeparatorIds/orderSeparatorId were removed: use orderIds',
                      str(ctx.exception))
        self.assertEqual(session.calls, [])


class AutoProlongSelectionTest(unittest.TestCase):
    """
    autoprolong/* выбирает прокси ровно так же, как prolong/* (одна и та же раскладка по
    типу), а у resident выбора нет вовсе: единица правки — весь пакет.
    """

    PROXY_ID = '68b1f0c4e13a4c0f1a2b3c4d'
    ORDER_ID = '6a248de4717805635cf6057d'

    def test_per_proxy_types_use_ids_and_ips(self):
        api, session = make_api([envelope({'autoProlong': True}), envelope({'total': 1})])
        api.autoProlongEnable('ipv4', [self.PROXY_ID], '1m', paymentId='balance')
        self.assertEqual(session.last['json'], {
            'ids': [self.PROXY_ID], 'periodId': '1m', 'paymentId': 'balance'})
        api.autoProlongCalc('mobile', ['10.0.0.1:8000:9000'], '1m', paymentId='balance')
        self.assertEqual(session.last['json'], {
            'ips': ['10.0.0.1:8000:9000'], 'periodId': '1m', 'paymentId': 'balance'})

    def test_whole_order_types_use_order_ids(self):
        for proxy_type in ('ipv6', 'mix', 'mix_isp'):
            with self.subTest(type=proxy_type):
                api, session = make_api([envelope({'total': 1}),
                                         envelope({'autoProlong': False})])
                api.autoProlongCalc(proxy_type, [self.ORDER_ID], '1m', paymentId='balance')
                self.assertEqual(session.last['json'], {
                    'orderIds': [self.ORDER_ID], 'periodId': '1m', 'paymentId': 'balance'})
                api.autoProlongDisable(proxy_type, [self.ORDER_ID])
                self.assertTrue(
                    session.last['url'].endswith('autoprolong/disable/' + proxy_type))
                self.assertEqual(session.last['json'], {'orderIds': [self.ORDER_ID]})

    def test_coupon_is_never_sent(self):
        api, session = make_api([envelope({'total': 1})])
        api.autoProlongCalc('ipv4', [self.PROXY_ID], '1m', paymentId='balance', coupon='SALE10')
        self.assertNotIn('coupon', session.last['json'])

    def test_proxy_ids_and_addresses_are_not_combined(self):
        api, session = make_api()
        with self.assertRaises(ValueError):
            api.autoProlongDisable('isp', [self.PROXY_ID, '1.2.3.4'])
        self.assertEqual(session.calls, [])

    def test_resident_sends_no_selection(self):
        api, session = make_api([envelope({'total': 3.5}), envelope({'autoProlong': True}),
                                 envelope({'autoProlong': False}), envelope({'total': 3.5})])
        api.autoProlongCalc('resident', paymentId='balance')
        self.assertEqual(session.last['json'], {'paymentId': 'balance'})
        api.autoProlongEnable('resident', paymentId='balance', tarifId='trial')
        self.assertEqual(session.last['json'], {'paymentId': 'balance', 'tarifId': 'trial'})
        api.autoProlongDisable('resident')
        self.assertEqual(session.last['json'], {})
        # Пустой выбор — не выбор: обобщённый клиент может слать его для любого типа.
        api.autoProlongCalc('Resident', [], paymentId='balance', ips=(), orderIds='',
                            options={'ids': []})
        self.assertEqual(session.last['json'], {'paymentId': 'balance'})

    def test_paddle_subscription_needs_no_subscription_id_locally(self):
        """
        paddle_subscription без subscriptionId уходит на сервер: с одной привязанной картой он
        берёт её сам, а сколько карт на аккаунте, знает только он. Заданный subscriptionId
        отправляется как есть.
        """
        api, session = make_api([envelope({'total': 1}), envelope({'autoProlong': True}),
                                 envelope({'autoProlong': True}), envelope({'autoProlong': True})])
        api.autoProlongCalc('ipv4', [self.PROXY_ID], '1m', paymentId='paddle_subscription')
        self.assertEqual(session.last['json'], {
            'ids': [self.PROXY_ID], 'periodId': '1m', 'paymentId': 'paddle_subscription'})
        api.autoProlongEnable('resident', paymentId='paddle_subscription')
        self.assertEqual(session.last['json'], {'paymentId': 'paddle_subscription'})
        api.setPaymentCode('paddle_subscription')
        api.autoProlongEnable('ipv6', [self.ORDER_ID], '1m')
        self.assertEqual(session.last['json'], {
            'orderIds': [self.ORDER_ID], 'periodId': '1m', 'paymentCode': 'paddle_subscription'})
        api.autoProlongEnable('mix', [self.ORDER_ID], '1m', subscriptionId='sub_1')
        self.assertEqual(session.last['json']['subscriptionId'], 'sub_1')

    def test_resident_with_selection_is_rejected_locally(self):
        """
        Любой выбор у resident — ids, ips или orderIds — сервер отбивает одним текстом
        ("[ids] is not applicable for resident: ..."), а молча выбросить его нельзя: disable
        с адресом снял бы автопродление со всего пакета.
        """
        server_text = ('[ids] is not applicable for resident: auto-prolong applies to the '
                       'whole package')
        calls = (
            lambda api: api.autoProlongDisable('resident', ['1.2.3.4']),
            lambda api: api.autoProlongEnable('resident', [self.PROXY_ID], paymentId='balance'),
            lambda api: api.autoProlongCalc('resident', paymentId='balance',
                                            orderIds=[self.ORDER_ID]),
            lambda api: api.autoProlongCalc('resident', paymentId='balance',
                                            options={'ids': [self.PROXY_ID]}),
        )
        for index, call in enumerate(calls):
            with self.subTest(call=index):
                api, session = make_api()
                with self.assertRaises(ValueError) as ctx:
                    call(api)
                self.assertIn(server_text, str(ctx.exception))
                self.assertEqual(session.calls, [])

    def test_enable_and_disable_answers_carry_ids_and_order_ids(self):
        """
        В ответе enable/disable — ids (затронутые прокси) и orderIds (их заказы); у resident
        оба списка пусты.
        """
        data = {'autoProlong': True, 'quantity': 100, 'ids': [self.PROXY_ID],
                'orderIds': [self.ORDER_ID], 'days': 30, 'paymentId': 'balance',
                'chargeDate': '2026-09-14 12:00:00', 'dateEnd': '2026-09-15 12:00:00'}
        resident = {'autoProlong': False, 'quantity': 1, 'ids': [], 'orderIds': [],
                    'days': 30, 'paymentId': None, 'chargeDate': None,
                    'dateEnd': '2026-10-15 12:00:00'}
        api, _ = make_api([envelope(data), envelope(resident)])
        result = api.autoProlongEnable('ipv6', [self.ORDER_ID], '1m', paymentId='balance')
        self.assertEqual(result['ids'], [self.PROXY_ID])
        self.assertEqual(result['orderIds'], [self.ORDER_ID])
        result = api.autoProlongDisable('resident')
        self.assertEqual(result['ids'], [])
        self.assertEqual(result['orderIds'], [])


class OrderMixIdentifierTest(unittest.TestCase):
    """
    Код MIX-пакета должен уезжать в mixId: сервер ищет пакет и резолвит его тег только в
    этом поле.
    """

    def test_package_code_goes_into_mix_id(self):
        api, session = make_api([envelope({'total': 10})])
        api.orderCalcMix('europe-2-mix_IPv4', '1m', 10)
        self.assertEqual(session.last['json'], {
            'sectionCode': 'mix', 'mixId': 'europe-2-mix_IPv4',
            'periodId': '1m', 'quantity': 10})
        self.assertNotIn('countryId', session.last['json'])


class OrderListTest(unittest.TestCase):
    """
    Фильтры запроса и поля ответа order/list называются в snake_case (start_date,
    is_extend, …). SDK передаёт имена ровно так, как их дал вызывающий: переименование
    сломало бы вызов молча — запрос ушёл бы, а фильтр не применился.
    """

    def test_snake_case_filter_names_are_sent_as_given(self):
        api, session = make_api([envelope({'metadata': {}, 'items': []})])
        api.orderList(order_id='ORDER_OBJECT_ID', start_date='01.06.2023',
                      end_date='30.06.2023', status='PAYED', is_extend='Y',
                      auto_order='N', page=1, limit=20, sort_by='date_insert',
                      order='desc')
        self.assertEqual(session.last['method'], 'GET')
        self.assertTrue(session.last['url'].endswith('order/list'))
        self.assertEqual(session.last['params'], {
            'order_id': 'ORDER_OBJECT_ID', 'start_date': '01.06.2023',
            'end_date': '30.06.2023', 'status': 'PAYED', 'is_extend': 'Y',
            'auto_order': 'N', 'page': 1, 'limit': 20, 'sort_by': 'date_insert',
            'order': 'desc'})

    def test_no_filters_means_no_params(self):
        api, session = make_api([envelope({'metadata': {}, 'items': []})])
        api.orderList()
        self.assertEqual(session.last['params'], {})

    def test_none_filters_are_dropped(self):
        api, session = make_api([envelope({'metadata': {}, 'items': []})])
        api.orderList(order_id=None, status='NOT_PAYED')
        self.assertEqual(session.last['params'], {'status': 'NOT_PAYED'})


#: Аргументы таблицы вызовов CallPaymentWinsTest.CALLS.
SEO = {'customTargetName': 'seo'}
OPERATOR = {'operatorId': 'op', 'rotationId': 5}
MIX = 'europe-2-mix_IPv4'
PROXY_ID = '68b1f0c4e13a4c0f1a2b3c4d'
ORDER_ID = '6a248de4717805635cf6057d'


class CallPaymentWinsTest(unittest.TestCase):
    """
    setPaymentId()/setPaymentCode() — значение по умолчанию: непустой paymentId ИЛИ
    paymentCode в самом вызове убирает пару клиента из запроса целиком. Раньше они
    смешивались, и "code старше id" отдавал победу коду клиента: prolongMake(...,
    paymentId='balance') после setPaymentCode('paddle_subscription') списывал с карты.
    """

    #: Все места, где подмешивается платёжка клиента: (путь, вызов с платёжкой вызова).
    CALLS = (
        ('order/calc', lambda api, pay: api.orderCalcIpv4('USA', '1m', 1, **SEO, **pay)),
        ('order/calc', lambda api, pay: api.orderCalcIsp('USA', '1m', 1, **SEO, **pay)),
        ('order/calc', lambda api, pay: api.orderCalcMix(MIX, '1m', 1, **pay)),
        ('order/calc', lambda api, pay: api.orderCalcIpv6('USA', '1m', 1, **SEO, **pay)),
        ('order/calc', lambda api, pay: api.orderCalcMobile('USA', '1m', 1, **OPERATOR, **pay)),
        ('order/calc', lambda api, pay: api.orderCalcResident('1-gb', **pay)),
        ('order/make', lambda api, pay: api.orderMakeIpv4('USA', '1m', 1, **SEO, **pay)),
        ('order/make', lambda api, pay: api.orderMakeIsp('USA', '1m', 1, **SEO, **pay)),
        ('order/make', lambda api, pay: api.orderMakeMix(MIX, '1m', 1, **pay)),
        ('order/make', lambda api, pay: api.orderMakeMixByCode(MIX, '1m', 1, **pay)),
        ('order/make', lambda api, pay: api.orderMakeIpv6('USA', '1m', 1, **SEO, **pay)),
        ('order/make', lambda api, pay: api.orderMakeMobile('USA', '1m', 1, **OPERATOR, **pay)),
        ('order/make', lambda api, pay: api.orderMakeResident('1-gb', **pay)),
        ('order/make', lambda api, pay: api.orderMakeIpv4(
            {'countryId': 'USA', 'periodId': '1m', 'quantity': 1, **SEO, **pay})),
        ('order/make', lambda api, pay: api.orderMakeResident('1-gb', options=dict(pay))),
        ('prolong/calc/ipv4', lambda api, pay: api.prolongCalc('ipv4', ['1.2.3.4'], '1m', **pay)),
        ('prolong/make/ipv6', lambda api, pay: api.prolongMake('ipv6', [ORDER_ID], '1m', **pay)),
        ('prolong/make/isp', lambda api, pay: api.prolongMake(
            'isp', {'ids': [PROXY_ID], 'periodId': '1m', **pay})),
        ('autoprolong/calc/mix', lambda api, pay: api.autoProlongCalc(
            'mix', [ORDER_ID], '1m', options=dict(pay))),
        ('autoprolong/enable/ipv4', lambda api, pay: api.autoProlongEnable(
            'ipv4', ['1.2.3.4'], '1m', **pay)),
        ('autoprolong/enable/resident', lambda api, pay: api.autoProlongEnable('resident', **pay)),
    )

    def check(self, client_setter, client_value, call_payment, expected):
        for index, (path, call) in enumerate(self.CALLS):
            with self.subTest(call=index, path=path):
                api, session = make_api([envelope({'total': 1})])
                getattr(api, client_setter)(client_value)
                call(api, call_payment)
                self.assertTrue(session.last['url'].endswith(path))
                body = session.last['json']
                for key in ('paymentId', 'paymentCode'):
                    if key in expected:
                        self.assertEqual(body.get(key), expected[key])
                    else:
                        self.assertNotIn(key, body)

    def test_call_payment_id_drops_the_client_payment_code(self):
        self.check('setPaymentCode', 'paddle_subscription', {'paymentId': 'balance'},
                   {'paymentId': 'balance'})

    def test_call_payment_code_drops_the_client_payment_id(self):
        self.check('setPaymentId', 'balance', {'paymentCode': 'paddle_subscription'},
                   {'paymentCode': 'paddle_subscription'})

    def test_client_payment_is_used_when_the_call_has_none(self):
        """Пустое значение в вызове (None, '', пробелы) — "не передано": берётся клиентская."""
        for blank in ({}, {'paymentId': None}, {'paymentId': ''}, {'paymentCode': '  '},
                      {'paymentId': '', 'paymentCode': None}):
            with self.subTest(call_payment=blank):
                self.check('setPaymentCode', 'balance', blank, {'paymentCode': 'balance'})

    def test_code_still_wins_over_id_within_the_call(self):
        api, session = make_api([envelope({'orderId': 'O1'})])
        api.setPaymentId('CLIENT_PAYMENT_ID')
        api.orderMakeResident('1-gb', paymentId='CALL_PAYMENT_ID', paymentCode='balance')
        self.assertEqual(session.last['json'], {
            'sectionCode': 'resident', 'tarifId': '1-gb', 'paymentCode': 'balance'})

    def test_snake_case_payment_id_counts_for_autoprolong(self):
        api, session = make_api([envelope({'autoProlong': True})])
        api.setPaymentCode('paddle_subscription')
        api.autoProlongEnable('ipv4', [PROXY_ID], '1m', payment_id='balance')
        self.assertEqual(session.last['json'], {
            'ids': [PROXY_ID], 'periodId': '1m', 'payment_id': 'balance'})

    def test_order_make_with_a_dict_body_is_sent_as_given(self):
        api, session = make_api([envelope({'orderId': 'O1'})])
        api.setPaymentCode('paddle_subscription')
        api.orderMake({'sectionCode': 'resident', 'tarifId': '1-gb', 'paymentId': 'balance'})
        self.assertEqual(session.last['json'], {
            'sectionCode': 'resident', 'tarifId': '1-gb', 'paymentId': 'balance'})

    def test_balance_add_call_payment_wins(self):
        api, session = make_api([envelope({'url': 'https://pay/1'})])
        api.setPaymentCode('paddle_subscription')
        api.setPaymentId('CLIENT_PAYMENT_ID')
        api.balanceAdd(10, 'CALL_PAYMENT_ID')
        self.assertEqual(session.last['json'], {'summ': 10, 'paymentId': 'CALL_PAYMENT_ID'})

    def test_balance_add_call_code_does_not_borrow_the_client_id(self):
        """Код в вызове: клиентский id не подставляется, а balance/add кодов не резолвит."""
        api, session = make_api()
        api.setPaymentId('CLIENT_PAYMENT_ID')
        with self.assertRaises(ValueError) as ctx:
            api.balanceAdd(10, paymentCode='cryptomus')
        self.assertIn('does not resolve paymentCode', str(ctx.exception))
        self.assertEqual(session.calls, [])

    def test_balance_add_blank_call_payment_falls_back_to_the_client(self):
        for blank in ('', '  '):
            with self.subTest(payment_id=blank):
                api, session = make_api([envelope({'url': 'https://pay/2'})])
                api.setPaymentId('CLIENT_PAYMENT_ID')
                api.balanceAdd(5, blank, paymentCode='')
                self.assertEqual(session.last['json'],
                                 {'summ': 5, 'paymentId': 'CLIENT_PAYMENT_ID'})


class KeyRedactionTest(unittest.TestCase):
    """
    Ключ стоит в пути URL. Ни текст ApiError, ни его repr/traceback/сериализация, ни body,
    errors и __cause__ не должны его содержать — ни как есть, ни URL-кодированным, ни в нижнем
    регистре (фронт-404 стейджа отдаёт путь в нижнем регистре).
    """

    KEY = 'Sec+ret/Key=42'
    ENCODED = 'Sec%2Bret%2FKey%3D42'
    SECRETS = (KEY, ENCODED, 'Sec%2Bret/Key%3D42')

    def api(self, responses=None, **config):
        return make_api(responses, key=self.KEY, **config)

    def assertNoKey(self, error):
        rendered = (
            str(error), repr(error),
            ''.join(traceback.format_exception(type(error), error, error.__traceback__)),
            json.dumps(vars(error), default=repr),
        )
        for text in rendered:
            for secret in self.SECRETS:
                self.assertNotIn(secret.lower(), text.lower())
        dumped = pickle.dumps(error).lower()
        for secret in self.SECRETS:
            self.assertNotIn(secret.lower().encode(), dumped)

    def test_network_error_on_a_closed_port(self):
        """Настоящий requests: "Max retries exceeded with url: /personal/api/v2/<key>/..."."""
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        api = Api({'key': self.KEY, 'timeout': 0.5, 'rate_limit': False,
                   'base_url': 'http://127.0.0.1:{}/personal/api/v2/'.format(port)})
        with api, self.assertRaises(ApiError) as ctx:
            api.balance()
        error = ctx.exception
        self.assertIsNone(error.http_status)
        self.assertIn('/personal/api/v2/***/balance/get', str(error))
        # Причина — копия исключения requests того же вида, без request/response; сырое
        # исключение не прицеплено ни как __cause__, ни как __context__.
        self.assertIsInstance(error.__cause__, requests.ConnectionError)
        self.assertIsNone(error.__cause__.request)
        self.assertIsNone(error.__context__)
        self.assertNoKey(error)
        self.assertNoKey(error.__cause__)

    def test_timeout_keeps_its_type_without_the_request(self):
        class Request:
            url = 'https://proxy-seller.com/personal/api/v2/{}/order/make'.format(self.ENCODED)

        api, _ = self.api([requests.ReadTimeout(
            "HTTPSConnectionPool(host='proxy-seller.com', port=443): Read timed out. "
            "(read timeout=120)", request=Request())])
        with self.assertRaises(ApiError) as ctx:
            api.orderMakeResident('1-gb')
        cause = ctx.exception.__cause__
        self.assertIsInstance(cause, requests.Timeout)
        self.assertIsNone(cause.request)
        self.assertIn('Read timed out', str(ctx.exception))
        self.assertNoKey(ctx.exception)

    def test_html_404_echoing_the_lowercase_path(self):
        page = ('<html><h1>404 Not Found</h1><p>/personal/api/v2/{}/balance/get</p>'
                '</html>').format(self.ENCODED.lower())
        api, _ = self.api([FakeResponse(
            NO_JSON, status_code=404, headers={'Content-Type': 'text/html; charset=UTF-8'},
            text=page, content=page.encode())])
        with self.assertRaises(ApiError) as ctx:
            api.balance()
        self.assertEqual(ctx.exception.http_status, 404)
        self.assertIn('/personal/api/v2/***/balance/get', str(ctx.exception))
        self.assertIn('***', ctx.exception.body)
        self.assertNoKey(ctx.exception)

    def test_json_500_echoing_the_path(self):
        body = {'error': 'Internal Server Error', 'status': 500,
                'message': 'No handler for /personal/api/v2/{}/order/make'.format(self.ENCODED),
                'path': '/personal/api/v2/{}/order/make'.format(self.ENCODED.lower()),
                'key': self.KEY}
        api, _ = self.api([FakeResponse(body, status_code=500)])
        with self.assertRaises(ApiError) as ctx:
            api.orderMakeResident('1-gb')
        error = ctx.exception
        self.assertEqual(error.http_status, 500)
        self.assertEqual(str(error), 'No handler for /personal/api/v2/***/order/make')
        self.assertEqual(error.body['path'], '/personal/api/v2/***/order/make')
        self.assertEqual(error.errors[0]['key'], '***')
        self.assertNoKey(error)

    def test_envelope_error_and_bytes_body(self):
        api, _ = self.api([
            error_envelope([{'message': 'Bad key {}'.format(self.KEY), 'code': 0,
                             'customData': {'path': self.ENCODED}}]),
            FakeResponse(NO_JSON, status_code=502,
                         headers={'Content-Type': 'application/octet-stream'},
                         content=b'upstream /personal/api/v2/' + self.ENCODED.lower().encode())])
        with self.assertRaises(ApiError) as ctx:
            api.authList()
        self.assertEqual(str(ctx.exception), 'Bad key ***')
        self.assertEqual(ctx.exception.custom_data, {'path': '***'})
        self.assertEqual(ctx.exception.customData, {'path': '***'})
        self.assertNoKey(ctx.exception)
        with self.assertRaises(ApiError) as ctx:
            api.authList()
        self.assertEqual(ctx.exception.body, b'upstream /personal/api/v2/***')
        self.assertNoKey(ctx.exception)

    def test_client_representation_has_no_key(self):
        api, _ = self.api()
        self.assertNotIn(self.ENCODED.lower(), repr(api).lower())
        self.assertNotIn(self.KEY.lower(), str(api).lower())


class StrictMoneyAndWriteSuccessTest(unittest.TestCase):
    """
    money и write: успех — только конверт со status "success". status "error" с data и пустым
    errors[] легален только у */calc (чтение); HTTP 2xx без конверта у money/write — ошибка
    "the request may have been executed". Чтения и файлы разбираются как раньше.
    """

    UNEXPECTED = 'no JSON envelope); the request may have been executed - check before retrying'

    @staticmethod
    def html(body, status=200):
        return FakeResponse(NO_JSON, status_code=status, headers={'Content-Type': 'text/html'},
                            text=body, content=body.encode())

    NO_ENVELOPE = {
        'html': lambda: StrictMoneyAndWriteSuccessTest.html('<html>Bad gateway</html>'),
        'empty json': lambda: FakeResponse(NO_JSON, content=b''),
        '204': lambda: FakeResponse(NO_JSON, status_code=204, headers={}, content=b''),
        'truncated json': lambda: FakeResponse(NO_JSON, content=b'{"status": "succ'),
        'json list': lambda: FakeResponse([{'orderId': 'O1'}]),
        'json string': lambda: FakeResponse('ok'),
        'json null': lambda: FakeResponse(None),
        'object without status': lambda: FakeResponse({'orderId': 'O1', 'total': 10}),
    }

    MONEY_AND_WRITE = (
        ('order/make', lambda api: api.orderMakeResident('1-gb')),
        ('prolong/make/ipv4', lambda api: api.prolongMake('ipv4', ['1.2.3.4'], '1m')),
        ('balance/add', lambda api: api.balanceAdd(10, 'PAYMENT_ID')),
        ('auth/add', lambda api: api.authAdd('ORDER-1')),
        ('autoprolong/enable/ipv4', lambda api: api.autoProlongEnable(
            'ipv4', ['1.2.3.4'], '1m', paymentId='balance')),
        ('residentsubuser/delete', lambda api: api.residentSubUserDelete('KEY')),
    )

    def test_answer_without_an_envelope_is_an_error(self):
        for path, call in self.MONEY_AND_WRITE:
            for kind, response in self.NO_ENVELOPE.items():
                with self.subTest(path=path, response=kind):
                    api, session = make_api([response()])
                    with self.assertRaises(ApiError) as ctx:
                        call(api)
                    self.assertTrue(session.last['url'].endswith(path))
                    self.assertIn('Unexpected response to ' + path, str(ctx.exception))
                    self.assertIn(self.UNEXPECTED, str(ctx.exception))
                    self.assertEqual(ctx.exception.http_status, response().status_code)
                    self.assertEqual(ctx.exception.errors, [])

    def test_unexpected_body_is_redacted_and_truncated(self):
        page = '<html>' + 'x' * 2000 + '/personal/api/v2/api-key/order/make</html>'
        api, _ = make_api([self.html('<p>/personal/api/v2/api-key/order/make</p>'),
                           self.html(page)])
        with self.assertRaises(ApiError) as ctx:
            api.orderMakeResident('1-gb')
        self.assertEqual(ctx.exception.body, '<p>/personal/api/v2/***/order/make</p>')
        with self.assertRaises(ApiError) as ctx:
            api.orderMakeResident('1-gb')
        self.assertEqual(len(ctx.exception.body), Api.UNEXPECTED_BODY_LIMIT)
        self.assertTrue(page.startswith(ctx.exception.body))

    def test_error_status_with_data_and_no_errors_fails_make_and_write(self):
        for path, call in self.MONEY_AND_WRITE:
            with self.subTest(path=path):
                api, _ = make_api([envelope({'orderId': 'O1', 'total': 10}, status='error')])
                with self.assertRaises(ApiError) as ctx:
                    call(api)
                self.assertEqual(str(ctx.exception), 'Client API returned an error')
                self.assertEqual(ctx.exception.http_status, 200)
                self.assertEqual(ctx.exception.body['data'], {'orderId': 'O1', 'total': 10})

    def test_errors_in_the_envelope_still_raise_as_before(self):
        api, _ = make_api([error_envelope(
            [{'message': 'Insufficient funds on balance', 'code': 16}])])
        with self.assertRaises(ApiError) as ctx:
            api.orderMakeResident('1-gb')
        self.assertEqual(ctx.exception.code, 16)

    def test_calc_with_insufficient_funds_still_returns_data(self):
        warning = {'warning': 'Insufficient funds. Total $3.00. Not enough $7.00', 'total': 10}
        cases = (
            lambda api: api.prolongCalc('ipv4', ['1.2.3.4'], '1m'),
            lambda api: api.autoProlongCalc('ipv4', ['1.2.3.4'], '1m', paymentId='balance'),
            lambda api: api.autoProlongCalc('resident', paymentId='balance'),
            lambda api: api.orderCalc({'sectionCode': 'resident', 'tarifId': '1-gb'}),
        )
        for index, call in enumerate(cases):
            with self.subTest(call=index):
                api, _ = make_api([envelope(warning, status='error')])
                self.assertEqual(call(api), warning)

    def test_reads_and_files_are_parsed_as_before(self):
        api, _ = make_api([
            self.html('<html>plain page</html>'),
            FakeResponse({'items': []}),
            envelope({'items': [1]}, status='error'),
            text_file('1.2.3.4:8080'),
            json_file('[{"code": "US"}]'),
        ])
        self.assertEqual(api.request('GET', 'proxy/list'), '<html>plain page</html>')
        self.assertEqual(api.request('GET', 'proxy/list/ipv4'), {'items': []})
        self.assertEqual(api.proxyList('ipv4'), {'items': [1]})
        self.assertEqual(api.proxyDownload('ipv4', ext='txt'), '1.2.3.4:8080')
        self.assertEqual(api.residentGeo(), b'[{"code": "US"}]')

    def test_delete_not_found_inside_a_success_envelope_is_kept(self):
        cases = (
            (lambda api: api.residentSubUserDelete('KEY'), {'status': 'not-found'}),
            (lambda api: api.residentSubUserListDelete('KEY', 1), {'status': 'not-found'}),
            (lambda api: api.residentListDelete(1), {'status': 'delete'}),
        )
        for index, (call, expected) in enumerate(cases):
            with self.subTest(call=index):
                payload = json.dumps(expected) if expected['status'] == 'not-found' else 'delete'
                api, _ = make_api([envelope(payload)])
                self.assertEqual(call(api), expected)


class MoneyTimeoutTest(unittest.TestCase):
    """
    money (order/make, prolong/make/{type}, balance/add) ждёт до 120 с, остальное — прежние
    30 с; заданный общий таймаут длиннее — money берёт его. Явный timeout вызова — как есть.
    """

    MONEY = (
        lambda api: api.orderMakeResident('1-gb'),
        lambda api: api.prolongMake('ipv4', ['1.2.3.4'], '1m'),
        lambda api: api.balanceAdd(10, 'PAYMENT_ID'),
    )
    OTHER = (
        lambda api: api.balance(),
        lambda api: api.orderCalc({'sectionCode': 'resident', 'tarifId': '1-gb'}),
        lambda api: api.authAdd('ORDER-1'),
        lambda api: api.autoProlongEnable('ipv4', ['1.2.3.4'], '1m', paymentId='balance'),
    )

    @staticmethod
    def ok():
        return envelope({'summ': 1, 'url': 'https://pay', 'orderId': 'O1'})

    def timeouts(self, calls, **config):
        api, session = make_api([self.ok() for _ in calls], **config)
        for call in calls:
            call(api)
        return [call['timeout'] for call in session.calls]

    def test_defaults(self):
        self.assertEqual(self.timeouts(self.MONEY), [120, 120, 120])
        self.assertEqual(self.timeouts(self.OTHER), [30, 30, 30, 30])
        api, _ = make_api()
        self.assertEqual((api.timeout, api.money_timeout), (30, 120))

    def test_money_uses_the_longer_of_both(self):
        self.assertEqual(self.timeouts(self.MONEY, timeout=10), [120, 120, 120])
        self.assertEqual(self.timeouts(self.OTHER, timeout=10), [10, 10, 10, 10])
        self.assertEqual(self.timeouts(self.MONEY, timeout=300), [300, 300, 300])
        self.assertEqual(self.timeouts(self.MONEY, request_options={'timeout': 200}),
                         [200, 200, 200])
        self.assertEqual(self.timeouts(self.OTHER, request_options={'timeout': 15}),
                         [15, 15, 15, 15])

    def test_money_timeout_option(self):
        for key in ('money_timeout', 'moneyTimeout'):
            with self.subTest(key=key):
                self.assertEqual(self.timeouts(self.MONEY, **{key: 200}), [200, 200, 200])
                self.assertEqual(self.timeouts(self.OTHER, **{key: 200}), [30, 30, 30, 30])
        self.assertEqual(self.timeouts(self.MONEY, money_timeout=None), [None, None, None])
        self.assertEqual(self.timeouts(self.MONEY, money_timeout=5), [30, 30, 30])

    def test_connect_read_pair_and_no_limit(self):
        self.assertEqual(self.timeouts(self.MONEY[:1], timeout=(3.05, 30)), [(3.05, 120)])
        self.assertEqual(self.timeouts(self.OTHER[:1], timeout=(3.05, 30)), [(3.05, 30)])
        self.assertEqual(self.timeouts(self.MONEY[:1], timeout=(3.05, None)), [(3.05, None)])
        self.assertEqual(self.timeouts(self.MONEY[:1], timeout=None), [None])

    def test_explicit_call_timeout_is_used_as_is(self):
        api, session = make_api([self.ok()])
        api.request('POST', 'order/make', json={'sectionCode': 'resident'}, timeout=7)
        self.assertEqual(session.last['timeout'], 7)

    def test_invalid_money_timeout(self):
        for value in (0, -1, '120', True, float('nan'), float('inf'), (5, 120)):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    make_api(money_timeout=value)
        with self.assertRaises(ValueError):
            make_api(money_timeout=60, moneyTimeout=60)


if __name__ == '__main__':
    unittest.main()
