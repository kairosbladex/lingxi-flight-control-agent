"""V1.4 静态标注目标解析：typed 适配器边界，不连接真实地图服务。"""
import json
from pathlib import Path
import unittest

from lingxi_flight_control.target_resolver import (
    TargetCandidate, TargetGeometry, TargetQuery, TargetResolverError,
    query_from_command, require_single_target, resolve_target, select_candidate,
)
from lingxi_flight_control.v13_extension import parse_command

FIXTURE = Path(__file__).resolve().parent.parent / 'fixtures' / 'v14-target-candidates-v1.json'


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def candidates():
    return [TargetCandidate(
        target_id=item['target_id'], name=item['name'],
        geometry_type=TargetGeometry(item['geometry_type']),
        longitude=item['longitude'], latitude=item['latitude'],
        altitude_m=item['altitude_m'], version=item['version'],
        source=item['source'], project_id=item['project_id'],
    ) for item in load_fixture()['candidates']]


class FakeResolver:
    """按名字/ID 过滤的假适配器；不访问网络。"""

    def __init__(self, items=None, *, error=None):
        self.items = candidates() if items is None else items
        self.error = error
        self.queries = []

    def resolve(self, query):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        result = []
        for item in self.items:
            if query.target_id is not None and item.target_id != query.target_id:
                continue
            if query.name is not None and item.name != query.name:
                continue
            result.append(item)
        return result


def query(name=None, target_id=None, intent='point_flight'):
    fixture = load_fixture()
    return TargetQuery(project_id=fixture['project_id'], actor_id='actor-1',
                       binding=fixture['binding'], intent=intent, name=name, target_id=target_id)


class TargetSelectionTests(unittest.TestCase):
    def test_unique_point_target_returns_coordinates_source_and_version(self):
        resolved = resolve_target(FakeResolver(), query(target_id='ann-water-tower-1'))
        self.assertEqual(resolved.target_id, 'ann-water-tower-1')
        self.assertEqual((resolved.longitude, resolved.latitude), (113.201, 23.101))
        self.assertEqual(resolved.altitude_m, 45.0)
        self.assertEqual(resolved.source, 'static-annotation')
        self.assertEqual(resolved.version[:8], '5f2c1d3e')
        self.assertEqual(resolved.binding, load_fixture()['binding'])
        self.assertEqual(resolved.project_id, 'project-1')
        self.assertEqual(resolved.actor_id, 'actor-1')

    def test_same_name_returns_candidates_instead_of_guessing(self):
        with self.assertRaises(TargetResolverError) as caught:
            resolve_target(FakeResolver(), query(name='1号水塔'))
        self.assertEqual(caught.exception.code, 'TARGET_AMBIGUOUS')
        self.assertEqual(len(caught.exception.candidates), 2)

    def test_missing_target_is_not_found(self):
        with self.assertRaises(TargetResolverError) as caught:
            resolve_target(FakeResolver(), query(name='不存在的塔'))
        self.assertEqual(caught.exception.code, 'TARGET_NOT_FOUND')

    def test_volume_target_is_unsupported(self):
        with self.assertRaises(TargetResolverError) as caught:
            resolve_target(FakeResolver(), query(name='立体大楼'))
        self.assertEqual(caught.exception.code, 'UNSUPPORTED_TARGET_GEOMETRY')

    def test_mixed_target_is_rejected_before_geometry_checks(self):
        with self.assertRaises(TargetResolverError) as caught:
            resolve_target(FakeResolver(), query(name='A塔与B河道'))
        self.assertEqual(caught.exception.code, 'MIXED_TARGET_UNSUPPORTED')

    def test_point_without_altitude_requires_user_supplied_height(self):
        with self.assertRaises(TargetResolverError) as caught:
            resolve_target(FakeResolver(), query(name='无高度点位'))
        self.assertEqual(caught.exception.code, 'TARGET_ALTITUDE_REQUIRED')

    def test_point_without_version_is_rejected(self):
        with self.assertRaises(TargetResolverError) as caught:
            resolve_target(FakeResolver(), query(name='无版本点位'))
        self.assertEqual(caught.exception.code, 'TARGET_VERSION_UNAVAILABLE')

    def test_line_and_polygon_only_belong_to_ground_planning(self):
        for name in ('B河道', '东片区农田'):
            with self.subTest(name=name):
                with self.assertRaises(TargetResolverError) as caught:
                    resolve_target(FakeResolver(), query(name=name))
                self.assertEqual(caught.exception.code, 'UNSUPPORTED_TARGET_GEOMETRY')
                # 地面规划链路可以取到线/面，但本仓不生成航线。
                resolved = resolve_target(FakeResolver(), query(name=name, intent='ground_plan'))
                self.assertIn(resolved.geometry_type, {TargetGeometry.LINE, TargetGeometry.POLYGON})

    def test_resolver_unavailable_blocks_instead_of_degrading(self):
        with self.assertRaises(TargetResolverError) as caught:
            resolve_target(None, query(name='1号水塔'))
        self.assertEqual(caught.exception.code, 'TARGET_RESOLVER_UNAVAILABLE')
        with self.assertRaises(TargetResolverError) as caught:
            resolve_target(FakeResolver(error=RuntimeError('private map detail')), query(name='1号水塔'))
        self.assertEqual(caught.exception.code, 'TARGET_RESOLVER_UNAVAILABLE')
        self.assertNotIn('private map detail', str(caught.exception))

    def test_multi_target_request_is_not_split_into_real_actions(self):
        with self.assertRaises(TargetResolverError) as caught:
            require_single_target([candidates()[0], candidates()[1]], intent='point_flight')
        self.assertEqual(caught.exception.code, 'MULTI_TARGET_UNSUPPORTED')
        require_single_target([candidates()[0]], intent='point_flight')

    def test_candidate_project_mismatch_is_rejected(self):
        foreign = TargetCandidate('ann-foreign', '外项目点位', TargetGeometry.POINT,
                                  113.0, 23.0, 40.0, 'v' * 64, 'static-annotation', 'project-2')
        with self.assertRaises(TargetResolverError) as caught:
            resolve_target(FakeResolver([foreign]), query(target_id='ann-foreign'))
        self.assertEqual(caught.exception.code, 'TARGET_BINDING_MISMATCH')

    def test_query_requires_exactly_one_identity(self):
        for name, target_id in (('1号水塔', 'ann-water-tower-1'), (None, None)):
            with self.subTest(name=name, target_id=target_id):
                with self.assertRaises(TargetResolverError) as caught:
                    query(name=name, target_id=target_id).validate()
                self.assertEqual(caught.exception.code, 'INVALID_TARGET_QUERY')


class ParseCommandBoundaryTests(unittest.TestCase):
    """parse_command 只抽取意图与原始目标，不伪造坐标。"""

    def test_fixture_commands_parse_without_coordinates_for_named_targets(self):
        for case in load_fixture()['parse_commands']:
            with self.subTest(text=case['text']):
                parsed = parse_command(case['text'])
                self.assertEqual(parsed, case['expect'])
                # 名称目标必须不带任何坐标；坐标指令则必须显式带经纬度。
                if case['expect'] and 'target' in case['expect'] and 'longitude' not in case['expect']:
                    self.assertNotIn('longitude', parsed)
                    self.assertNotIn('latitude', parsed)
                    self.assertNotIn('altitudeM', parsed)

    def test_named_target_command_becomes_a_query_without_coordinates(self):
        parsed = parse_command('去1号水塔')
        target_query = query_from_command(parsed, project_id='project-1', actor_id='actor-1',
                                          binding=load_fixture()['binding'], intent=parsed['action'])
        self.assertEqual(target_query.name, '1号水塔')
        self.assertIsNone(target_query.target_id)
        self.assertEqual(target_query.intent, 'point_flight')

    def test_coordinate_command_never_goes_through_target_resolution(self):
        parsed = parse_command('去东经118，北纬34，高50')
        self.assertEqual((parsed['longitude'], parsed['latitude'], parsed['altitudeM']),
                         (118.0, 34.0, 50.0))
        # 坐标指令不需要目标解析器；占位名不能被当成真实标注去查。
        with self.assertRaises(TargetResolverError) as caught:
            query_from_command(parsed, project_id='project-1', actor_id='actor-1',
                               binding=load_fixture()['binding'], intent='point_flight')
        self.assertEqual(caught.exception.code, 'INVALID_TARGET_QUERY')


if __name__ == '__main__':
    unittest.main()
