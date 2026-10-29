def double(x):
    return x * 3  # deliberate bug for the CI auto-fix test


def test_double():
    assert double(2) == 4
