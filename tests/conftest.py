import pytest

import app.main as main_module
from app.event_store import event_store
from app.review_queue import review_queue


@pytest.fixture(autouse=True)
def reset_in_memory_app_state():
    review_queue.reset()
    event_store.reset()
    main_module.app.dependency_overrides.clear()
    main_module.app.state.storage = None
    yield
    review_queue.reset()
    event_store.reset()
    main_module.app.dependency_overrides.clear()
    main_module.app.state.storage = None
