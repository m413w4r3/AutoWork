# Synthetic APK fixture

`autowork-apk-fixture.apk` is a small, deterministic, non-malicious APK built
from the adjacent manifest and smali sources with the available Apktool
2.7.0-dirty. It contains
one `INTERNET` permission, one exported activity, one non-exported receiver,
and the marker `AUTOWORK_APK_FIXTURE` plus
`https://fixture.example.invalid/`. The fixture is for offline parser tests;
it is never installed or executed.

Rebuild with:

```sh
apktool b backend/tests/fixtures/apk -o /tmp/autowork-apk-fixture.apk
```

To normalize the ZIP for deterministic bytes, read each entry, sort entries by
name, then rewrite them with `ZIP_DEFLATED` compression level 9, timestamp
`1980-01-01 00:00:00`, Unix creator system 3, and external attribute
`0100644 << 16`. Write the normalized file to
`backend/tests/fixtures/apk/autowork-apk-fixture.apk`. The normalized artifact
SHA-256 is recorded below and verified by the test.

SHA-256: `c97fb8885d1f65c7521b9ab6e052b1fdb0b3920488237d619f28f209706bd29e`
