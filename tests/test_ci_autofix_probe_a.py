def add(a, b):
    return a - b  # deliberate bug for the CI auto-fix test


def test_add():
    assert add(2, 2) == 4
