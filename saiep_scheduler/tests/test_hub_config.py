from saiep_hub_config import DURATION, MEMORY, options_from_form


def test_sizing_form_normalizes_memory_and_duration():
    assert MEMORY['1G'] == '1Gi'
    assert MEMORY['512M'] == '512Mi'
    assert DURATION['1h'] == 3600
    assert DURATION['1w'] == 7 * 24 * 3600


def test_sizing_form_defaults_and_truncation():
    options = options_from_form({
        'cpu': ['2'],
        'memory': ['4G'],
        'duration': ['1d'],
        'usage': ['x' * 3000],
    })
    assert options == {'cpu': '2', 'memory': '4G', 'duration': '1d', 'usage': 'x' * 2000}
