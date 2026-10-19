import uvicorn
from app.config import settings
from app.utils.helpers import setup_logging

if __name__ == "__main__":
    setup_logging()
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
    )
