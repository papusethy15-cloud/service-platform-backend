from typing import Any, Optional

def success_response(data: Any = None, message: str = "Success") -> dict:
    return {"success": True, "message": message, "data": data}

def error_response(message: str, errors: Optional[dict] = None) -> dict:
    return {"success": False, "message": message, "errors": errors or {}}
