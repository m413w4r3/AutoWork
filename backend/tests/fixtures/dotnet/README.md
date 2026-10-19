# Minimal .NET fixture

`tests/dotnet_fixture.py` carries the small PE/CLI fixture already used by the
local `monodis` tests. Its sole behavior is `Fixture.Main() => 7`; it has no
external code, resources, or runtime dependencies. `Fixture.cs` records the
source represented by the fixture. The integration test checks the returned
sample SHA-256 and the expected `ldc.i4.7` / `ret` body, so the evidence is tied
to these exact fixture bytes.

The source and fixture are project-created test data. ILSpy itself remains an
external MIT-licensed dependency and is independently pinned in the worker
image.
