"""Отказ проверки полей (422) не возвращает присланное и собирается из любой строки.

FastAPI по умолчанию кладёт в `detail[].input` значение, которое не прошло
проверку. До 2026-10-11 это давало:

- `POST /auth/login` с паролем длиннее 200 знаков возвращал этот пароль в теле
  ответа; `POST /auth/set-password` без поля `token` — тело целиком, с новым
  паролем; так же `POST /api/v1/dash/me/password` — текущий пароль;
- на одиночном суррогате (`"\\ud800"`: в JSON строка допустима, в UTF-8 её нет)
  ответ 422 не собирался вовсе — выходил 500. Вместе с ним в Sentry уходил текст
  исключения FastAPI, а он перечисляет те же `input`: пароль попадал туда и при
  выключенных локальных переменных и теле запроса.

Что здесь проверяется:

- ни на одном адресе входа и ни при каком виде отказа присланного в ответе нет;
- остаётся то, чем запрос можно поправить, — `type`, `loc`, `msg` и `ctx`:
  интерфейс читает `msg` и `loc` (`friendlyError`), по `ctx` берёт перевод;
- ответ собирается, когда суррогат стоит в значении, в имени поля, в теле;
- правило одно на все маршруты, и ни одна модель запроса не цитирует значение
  в тексте причины.

Отказ, который обработчик бросает сам (`HTTPException(422)`), сюда не относится:
его текст пишет наш код.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_core import PydanticCustomError
from sqlalchemy.orm import sessionmaker

from src import api as api_module
from src import rate_limit, storage, tenants
from src.storage import TenantUser

# То, чего в ответе быть не должно: стоит на месте пароля, токена, адреса.
SECRET = "S3CRETs3cretS3CRET"
KEPT = {"type", "loc", "msg", "ctx"}

ECHOED_BEFORE = [
    pytest.param(
        "/auth/login", {"login": "admin", "password": SECRET * 12}, id="login-password-too-long"
    ),
    pytest.param("/auth/login", {"login": SECRET * 12, "password": "x"}, id="login-too-long"),
    pytest.param("/auth/login", {"password": SECRET}, id="login-without-login"),
    pytest.param("/auth/login", {"login": SECRET, "password": 5}, id="login-password-is-a-number"),
    pytest.param(
        "/auth/login", {"login": "admin", "password": [SECRET]}, id="login-password-is-a-list"
    ),
    pytest.param(
        "/auth/login",
        {"login": "admin", "password": {"is": SECRET}},
        id="login-password-is-an-object",
    ),
    pytest.param("/auth/login", [SECRET], id="login-body-is-a-list"),
    pytest.param("/auth/login", SECRET, id="login-body-is-a-string"),
    pytest.param("/auth/set-password", {"new_password": SECRET}, id="set-password-without-token"),
    pytest.param(
        "/auth/set-password",
        {"token": SECRET * 30, "new_password": "long-enough"},
        id="set-password-token-too-long",
    ),
    pytest.param("/auth/set-password", {"token": [SECRET]}, id="set-password-token-is-a-list"),
    pytest.param("/auth/request", {"email": SECRET}, id="request-not-an-address"),
    pytest.param("/auth/request", {"address": SECRET}, id="request-without-email"),
]

# Тело — байтами: суррогат записан так, как его шлёт браузер.
WITH_A_SURROGATE = [
    pytest.param("/auth/set-password", b'{"new_password": "abc\\ud800def"}', id="value"),
    pytest.param(
        "/auth/login",
        b'{"login": "' + b"\\ud800" * 201 + b'", "password": "x"}',
        id="value-too-long",
    ),
    pytest.param("/auth/login", b'{"login": "admin", "password": ["\\ud800"]}', id="in-a-list"),
    pytest.param("/auth/login", b'{"\\ud800": "x"}', id="field-name"),
    pytest.param("/auth/login", b'["\\ud800"]', id="body-is-a-list"),
    pytest.param("/auth/login", b'"\\udc00"', id="body-is-a-string"),
    pytest.param("/auth/request", b'{"email": "\\ud800"}', id="address"),
]


@pytest.fixture
def sessions(monkeypatch, db_session) -> sessionmaker:
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed — JWT tests skipped")
    factory = sessionmaker(db_session.get_bind(), expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(storage, "make_session", lambda database_url=None: factory)
    monkeypatch.setenv("JWT_SECRET", "test-secret-very-long-not-for-prod-only")
    # Счёт запросов — в памяти процесса и с нуля: в CI рядом стоит Redis, и
    # счётчик в нём переживал бы тест.
    monkeypatch.setattr(rate_limit, "_redis_client", lambda: None)
    rate_limit._reset_memory_for_tests()
    return factory


@pytest.fixture
def client(sessions) -> TestClient:
    # Исключение обработчика — это ответ 500, а не падение теста: сверяем ответы.
    return TestClient(api_module.app, raise_server_exceptions=False)


@pytest.fixture
def signed_in(client, sessions) -> TestClient:
    with sessions() as session:
        tenant = tenants.get_or_create_default(session)
        user = TenantUser(tenant_id=tenant.id, email="owner@example.com", is_active=True)
        session.add(user)
        session.commit()
        cookie = api_module._make_jwt(user.id, user.tenant_id, user.email)
    client.cookies.set(api_module.COOKIE_NAME, cookie)
    return client


def _post(client: TestClient, path: str, body: Any) -> Response:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode("ascii")
    return client.post(path, content=raw, headers={"content-type": "application/json"})


def _assert_refusal_echoes_nothing(response: Response) -> list[dict]:
    assert response.status_code == 422, response.text
    assert SECRET not in response.text
    problems = response.json()["detail"]
    assert problems, "отказ без единой причины"
    for problem in problems:
        assert {"type", "loc", "msg"} <= set(problem) <= KEPT, problem
    return problems


@pytest.mark.parametrize(("path", "body"), ECHOED_BEFORE)
def test_a_refusal_does_not_return_what_was_sent(client, path, body):
    _assert_refusal_echoes_nothing(_post(client, path, body))


def test_a_cut_off_body_is_not_returned(client):
    cut_off = b'{"login": "admin", "password": "' + SECRET.encode()

    (problem,) = _assert_refusal_echoes_nothing(_post(client, "/auth/login", cut_off))

    assert problem["type"] == "json_invalid"


def test_the_current_password_is_not_returned(signed_in):
    refused = _post(signed_in, "/api/v1/dash/me/password", {"current_password": SECRET})

    (problem,) = _assert_refusal_echoes_nothing(refused)
    assert problem["loc"] == ["body", "new_password"]


def test_a_query_value_is_not_returned(signed_in):
    refused = signed_in.get("/api/v1/dash/alerts", params={"limit": SECRET})

    (problem,) = _assert_refusal_echoes_nothing(refused)
    assert problem["loc"] == ["query", "limit"]


def test_the_refusal_still_names_the_field_and_the_reason(client):
    refused = _post(client, "/auth/set-password", {"new_password": SECRET})

    # Ровно то, что читает интерфейс: `msg` и последний элемент `loc`.
    assert refused.json() == {
        "detail": [{"type": "missing", "loc": ["body", "token"], "msg": "Field required"}]
    }


@pytest.mark.parametrize(("path", "raw"), WITH_A_SURROGATE)
def test_a_lone_surrogate_gets_a_refusal_not_a_server_error(client, path, raw):
    refused = _post(client, path, raw)

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]


def test_the_app_refuses_through_this_handler():
    registered = api_module.app.exception_handlers[RequestValidationError]

    assert registered is api_module._validation_refusal


# ─── Правило на любом маршруте ───────────────────────────────────────────────
#
# Маршруты входа берут простые строки. Здесь — то, чего у них нет: лишнее поле,
# параметр правила, код причины.


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(default="abc", min_length=3)
    address: str = "a@example.com"
    prices: dict[str, int] = Field(default_factory=dict)

    @field_validator("address")
    @classmethod
    def _address(cls, value: str) -> str:
        if "@" not in value:
            # Так отказывает PR с единым правилом адреса: код причины — в `ctx`.
            raise PydanticCustomError(
                "email_address", "{reason}", {"reason": "Адрес не принят.", "problem": "domain"}
            )
        return value


@pytest.fixture
def strict() -> TestClient:
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, api_module._validation_refusal)

    @app.post("/strict")
    def take(payload: _Strict):
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b'{"\\ud800": "x"}', id="extra-field"),
        pytest.param(b'{"prices": {"\\ud800": "x"}}', id="key-of-a-mapping"),
    ],
)
def test_a_surrogate_in_a_name_pydantic_reports_is_refused_too(strict, raw):
    """Имя из запроса pydantic кладёт в `loc` — но суррогат в нём заменяет сам.

    На это опирается обработчик: без `input` суррогату в ответ попасть неоткуда,
    и собирать ответ особым способом не нужно.
    """
    refused = _post(strict, "/strict", raw)

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]


def test_the_parameters_of_the_rule_stay_in_the_refusal(strict):
    (problem,) = _assert_refusal_echoes_nothing(_post(strict, "/strict", {"code": "ab"}))

    assert problem["type"] == "string_too_short"
    assert problem["ctx"] == {"min_length": 3}


def test_the_reason_code_stays_in_the_refusal(strict):
    (problem,) = _assert_refusal_echoes_nothing(_post(strict, "/strict", {"address": SECRET}))

    assert problem == {
        "type": "email_address",
        "loc": ["body", "address"],
        "msg": "Адрес не принят.",
        "ctx": {"reason": "Адрес не принят.", "problem": "domain"},
    }


def _models_of_the_api() -> list[type[BaseModel]]:
    return sorted(
        (
            model
            for model in vars(api_module).values()
            if isinstance(model, type)
            and issubclass(model, BaseModel)
            and model.__module__ == api_module.__name__
        ),
        key=lambda model: model.__name__,
    )


# Значение на месте каждого поля: слишком длинное, пустое по смыслу, не того типа.
PROBES = [SECRET, SECRET * 40, f" {SECRET} ", f"{SECRET}\ud800", [SECRET], {"is": SECRET}]


@pytest.mark.parametrize("model", _models_of_the_api(), ids=lambda model: model.__name__)
async def test_no_model_quotes_the_value_in_its_reason(model):
    """`msg` пишет валидатор: `raise ValueError(f"плохой пароль {v}")` вернул бы пароль."""
    for probe in PROBES:
        try:
            model.model_validate({name: probe for name in model.model_fields})
        except ValidationError as refusal:
            errors = refusal.errors(include_url=False)
        else:
            continue
        response = await api_module._validation_refusal(None, RequestValidationError(errors))
        assert SECRET.encode() not in response.body, (model.__name__, response.body)


def test_the_sweep_sees_the_models_of_the_login_routes():
    names = {model.__name__ for model in _models_of_the_api()}

    assert {"PasswordLoginIn", "SetPasswordIn", "PasswordChangeIn", "AuthRequestIn"} <= names
