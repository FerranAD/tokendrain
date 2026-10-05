{
  lib,
  python312Packages,
  makeWrapper,
  coreutils,
  e2fsprogs,
  doCheck ? false,
}:
python312Packages.buildPythonApplication {
  pname = "tokendrain";
  version = "0.1.0";
  pyproject = true;
  src = lib.cleanSourceWith {
    src = ../.;
    filter =
      path: type:
      let
        relative = lib.removePrefix (toString ../. + "/") (toString path);
        top = builtins.head (lib.splitString "/" relative);
        name = baseNameOf path;
      in
      toString path == toString ../.
      || (
        builtins.elem top [
          "src"
          "tests"
          "pyproject.toml"
        ]
        && !(builtins.elem name [
          "__pycache__"
          ".pytest_cache"
          ".mypy_cache"
          ".ruff_cache"
        ])
        && !(lib.hasSuffix ".pyc" name)
      );
  };
  build-system = [ python312Packages.hatchling ];
  dependencies = with python312Packages; [
    fastapi
    uvicorn
    pydantic
    pydantic-settings
    sqlalchemy
    alembic
    aiosqlite
    httpx
    cryptography
    pyjwt
    croniter
    python-dotenv
    python-multipart
    greenlet
  ];
  nativeBuildInputs = [ makeWrapper ];
  makeWrapperArgs = [
    "--prefix"
    "PATH"
    ":"
    (lib.makeBinPath [
      coreutils
      e2fsprogs
    ])
  ];
  # Deployment builds retain import checks; pytest is enabled by checks.python.
  inherit doCheck;
  nativeCheckInputs = lib.optionals doCheck (
    [
      e2fsprogs
    ]
    ++ (with python312Packages; [
      pytestCheckHook
      pytest-asyncio
      asgi-lifespan
    ])
  );
  disabledTestMarks = [
    "kvm"
    "nix"
  ];
  pythonImportsCheck = [
    "tokendrain"
    "tokendrain_guestd"
  ];
  meta = {
    description = "Self-hosted autonomous Codex project supervisor";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
    mainProgram = "tokendraind";
  };
}
