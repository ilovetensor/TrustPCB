def pytest_configure(config):
    config.addinivalue_line("markers", "slow: runs the solver in Docker (minutes)")
