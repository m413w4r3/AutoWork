import dramatiq
from dramatiq.brokers.redis import RedisBroker

from cti_app.config import get_settings
from cti_app.logging import configure_logging

settings = get_settings()
configure_logging(settings.log_level)

broker = RedisBroker(url=settings.redis_url)  # type: ignore[no-untyped-call]
dramatiq.set_broker(broker)
