from .config import get_settings
from .webhook import create_app

app = create_app(get_settings())
