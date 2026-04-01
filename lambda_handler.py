"""AWS Lambda entry point — wraps the FastAPI app with Mangum."""
from mangum import Mangum

from booking_engine.api.app import create_app

app = create_app()
handler = Mangum(app, lifespan="auto")
