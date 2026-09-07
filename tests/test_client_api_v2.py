"""
Офлайн-проверки локальных гейтов SDK. Каждый ассерт повторяет поведение бэкенда
client-api v2 (client-api-service), а не README.

Запуск: python -m unittest discover
"""

import json
import unittest

from proxy_seller_user_api import Api, ApiError

from .support import FakeSession, envelope, error_envelope, json_file, text_file


def make_api(responses=None, **config):
    session = FakeSession(responses)
    options = {'key': 'API-KEY', 'session': session}
    options.update(config)
    return Api(options), session


class BaseUriTest(unittest.TestCase):
    def test_api_key_goes_into_path(self):
        api, _ = make_api()
        self.assertEqual(
            api.base_uri, 'https://proxy-seller.com/personal/api/v2/API-KEY/')

    def test_custom_base_url(self):
        api, _ = make_api(base_url='http://localhost:7995/personal/api/v2')
        self.assertEqual(api.base_uri, 'http://localhost:7995/personal/api/v2/API-KEY/')


class AssertTargetNameTest(unittest.TestCase):
    """ClientApiService: для ipv4/ipv6/isp цель обязательна, mix резолвится как в parseMixSelection."""

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
        # countryId = "packageId:quantity" — вторая форма parseMixSelection
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
        # fingerprint здесь не предмет проверки, но без него резидентский order/make
        # отбивается ЛОКАЛЬНО и до разбора конверта дело не доходит.
        api, _ = make_api([error_envelope([{'message': 'Incorrect goal', 'code': 14}])],
                          fingerprint='test-fingerprint')
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
    prolong/make при нехватке средств кладёт причину в errors[{code: 16}]
    (ProlongMakeResponseClientDto.ofInsufficientFunds), поэтому её разбирает общий код
    конверта — отдельной обёртки в SDK больше нет.

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

    def test_successful_prolong_still_returns_data(self):
        api, _ = make_api([envelope({
            'orderId': '68b1f0c4e13a4c0f1a2b3c4d', 'total': 10,
            'listBaseOrderNumbers': [], 'balance': 90})])
        result = api.prolongMake('ipv4', ids=['68b1f0c4e13a4c0f1a2b3c4d'], periodId='1m')
        self.assertEqual(result['orderId'], '68b1f0c4e13a4c0f1a2b3c4d')

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
    options ради кодов. У каждого *Id-поля на бэкенде есть фолбэк
    (normalizeOrderReferenceCodes): значение, не являющееся валидным id, резолвится как код,
    если соответствующий *Code пуст. Исключение — rotationId: он обязан быть ЧИСЛОМ МИНУТ
    (0 = "By Link"), '5m'/'10m' сервер отвергает.
    """

    def test_ipv4_example_passes_codes_positionally(self):
        api, session = make_api([envelope({'total': 10})])
        api.setPaymentId('PAYMENT_ID')
        api.orderCalcIpv4('USA', '1m', 2, customTargetName='seo', uptime=True)
        self.assertEqual(session.last['json'], {
            'sectionCode': 'ipv4', 'paymentId': 'PAYMENT_ID', 'countryId': 'USA',
            'periodId': '1m', 'quantity': 2, 'customTargetName': 'seo', 'uptime': True})

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

    def test_prolong_example_passes_codes_in_id_fields(self):
        api, session = make_api([envelope({'orderId': '68b1f0c4e13a4c0f1a2b3c4d'})])
        api.prolongMake('mix', orderSeparatorIds=['SEPARATOR_ID'], periodId='1m',
                        paymentId='balance', coupon='SALE10')
        self.assertTrue(session.last['url'].endswith('prolong/make/mix'))
        self.assertEqual(session.last['json'], {
            'orderSeparatorIds': ['SEPARATOR_ID'], 'periodId': '1m',
            'paymentId': 'balance', 'coupon': 'SALE10'})


class ProlongByAddressTest(unittest.TestCase):
    """
    Продление по самим адресам: их клиент видит в proxy/list, ObjectId — нет.
    Сервер при наличии ids игнорирует ips, поэтому пустой ids рядом с ips недопустим.
    """

    def test_addresses_go_into_ips(self):
        api, session = make_api([envelope({'orderId': '68b1f0c4e13a4c0f1a2b3c4d'})])
        api.prolongMake('ipv4', ['1.2.3.4', '5.6.7.8'], '1m')
        self.assertEqual(session.last['json'], {
            'ips': ['1.2.3.4', '5.6.7.8'], 'periodId': '1m', 'coupon': ''})
        self.assertNotIn('ids', session.last['json'])

    def test_objectids_go_into_ids(self):
        api, session = make_api([envelope({'orderId': '68b1f0c4e13a4c0f1a2b3c4d'})])
        api.prolongMake('ipv4', ['68b1f0c4e13a4c0f1a2b3c4d'], '1m')
        self.assertEqual(session.last['json'], {
            'ids': ['68b1f0c4e13a4c0f1a2b3c4d'], 'periodId': '1m', 'coupon': ''})
        self.assertNotIn('ips', session.last['json'])

    def test_mixed_list_is_routed_by_shape(self):
        api, session = make_api([envelope({'total': 1})])
        api.prolongCalc('ipv4', ['1.2.3.4', '68b1f0c4e13a4c0f1a2b3c4d'], '1m')
        self.assertEqual(session.last['json'], {
            'ids': ['68b1f0c4e13a4c0f1a2b3c4d'], 'ips': ['1.2.3.4'],
            'periodId': '1m', 'coupon': ''})

    def test_mobile_address_with_colons_is_an_address(self):
        """mobile: 'ip:port_http:port_socks' — двоеточия не мешают попасть в ips."""
        api, session = make_api([envelope({'total': 1})])
        api.prolongCalc('mobile', ['10.0.0.1:8000:9000'], '1m')
        self.assertEqual(session.last['json']['ips'], ['10.0.0.1:8000:9000'])

    def test_ipv6_address_is_the_ip_field_with_port(self):
        """
        ipv6: в поле 'ip' из proxy/list уже лежат шлюз и порт ('1.2.3.4:26000'),
        в 'ip_only' — один шлюз; двоеточие увозит адрес в ips как есть.
        """
        api, session = make_api([envelope({'total': 1})])
        api.prolongCalc('ipv6', ['1.2.3.4:26000'], '1m')
        self.assertEqual(session.last['json']['ips'], ['1.2.3.4:26000'])
        self.assertNotIn('ids', session.last['json'])

    def test_comma_string_and_blanks(self):
        api, session = make_api([envelope({'total': 1})])
        api.prolongCalc('ipv4', '1.2.3.4, 5.6.7.8 ,  ', '1m')
        self.assertEqual(session.last['json']['ips'], ['1.2.3.4', '5.6.7.8'])


class OrderMixIdentifierTest(unittest.TestCase):
    """
    Код MIX-пакета должен уезжать в mixId: parseMixSelection ищет пакет через
    findById(mixId), а тег в ObjectId переводит normalizeOrderReferenceCodes — тоже только
    для mixId.
    """

    def test_package_code_goes_into_mix_id(self):
        api, session = make_api([envelope({'total': 10})])
        api.orderCalcMix('europe-2-mix_IPv4', '1m', 10)
        self.assertEqual(session.last['json'], {
            'sectionCode': 'mix', 'mixId': 'europe-2-mix_IPv4',
            'periodId': '1m', 'quantity': 10})
        self.assertNotIn('countryId', session.last['json'])


if __name__ == '__main__':
    unittest.main()
