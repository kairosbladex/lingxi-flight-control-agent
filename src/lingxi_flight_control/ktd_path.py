"""Reuse the platform's path-route/with-clean checker; never adopt its reroute."""
from .flight_policy import PathCheck, MOVES, finite
from .geometry import relative_target


class KtdPathChecker:
    def __init__(self, transport):
        self.transport = transport

    def __call__(self, binding, action, parameters, state):
        # The platform DTO uses relative altitude, not WGS84 ellipsoid height.
        values = [state.get(key) for key in ('longitude', 'latitude', 'height_m')]
        if not all(finite(value) for value in values):
            return PathCheck('unavailable')
        lng, lat, height = values
        if not -180 <= lng <= 180 or not -90 <= lat <= 90:
            return PathCheck('unavailable')
        start = {'longitude': lng, 'latitude': lat, 'altitude': height, 'source': 'old'}
        end = dict(start, source='new')
        if action in MOVES:
            distance = parameters['distance_m']
            if action in {'move_up', 'move_down'}:
                end['altitude'] += distance * (1 if action == 'move_up' else -1)
            else:
                yaw = state.get('yaw_deg')
                if not finite(yaw):
                    return PathCheck('unavailable')
                end['longitude'], end['latitude'] = relative_target(lng, lat, yaw, action.removeprefix('move_'), distance)
        else:
            # Orbit needs a curve contract, not a chord that falsely proves a safe circle.
            return PathCheck('unavailable')
        response = self.transport.check_path(binding, [start, end])
        if response.status_code != 200 or response.payload.get('code') != 200:
            return PathCheck('unavailable')
        data = response.payload.get('data')
        if not isinstance(data, dict) or data.get('projectId') != binding.project_id or data.get('operatorId') != binding.actor_id:
            return PathCheck('unavailable')
        if data.get('snapshot') != [start, end]:
            return PathCheck('unavailable')
        segments, invalid = data.get('segment'), data.get('invalid')
        if not isinstance(invalid, list) or not isinstance(segments, list) or len(segments) != 1:
            return PathCheck('unavailable')
        segment = segments[0]
        if not isinstance(segment, dict) or segment.get('status') not in {'ok', 'ng'}:
            return PathCheck('unavailable')
        if invalid or segment['status'] == 'ng':
            return PathCheck('blocked')
        if segment.get('converge') != 'ln_outside':
            return PathCheck('unavailable')
        return PathCheck('clear')
