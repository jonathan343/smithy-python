# Flattened operation inputs

Status: implemented in the Java code generator. Regenerating a client changes
its calling convention; this does not change already published SDKs.

## Calling convention

Before regeneration:

```python
await sts.get_caller_identity(GetCallerIdentityInput())
await sts.assume_role(
    AssumeRoleInput(role_arn=role_arn, role_session_name="session")
)
```

After regeneration:

```python
await sts.get_caller_identity()
await sts.assume_role(role_arn=role_arn, role_session_name="session")
```

Methods have explicit typed, keyword-only parameters rather than `**kwargs` or a
runtime adapter. Python rejects unknown keywords and positional members; editors,
type checkers, `help()`, and `inspect.signature` can discover the parameters.

Flattening is **one level only**. Nested structures and unions remain generated
models, collections keep their element types, and outputs remain generated
output objects. For example, STS tags still use `Tag`:

```python
from aws_sdk_sts.models import Tag

await sts.assume_role(
    role_arn=role_arn,
    role_session_name="session",
    tags=[Tag(key="team", value="sdk")],
)
```

The generated method constructs the existing input dataclass before running the
existing request pipeline. Interceptors still receive that model. Serialization,
retries, authentication, transport, and error handling are unchanged. Explicitly
supplied nested models, collections, and streaming bodies are passed through
without conversion or copying by the method.

## Migration and compatibility

This is a source-compatibility break, not a wire-format change:

* Replace `operation(Input(member=value))` with `operation(member=value)`.
* Replace `operation(EmptyInput())` with `operation()`.
* Pass per-operation plugins by keyword: `operation(member=value, plugins=[...])`.
* Remove input-model imports when they are no longer used.
* If code already builds an input object elsewhere, forward its members
  explicitly, using the generated operation's parameter names.

Neither `operation(Input(...))` nor `operation(input=Input(...))` is supported.
Input models remain available for the pipeline and interceptor users, but are
not an alternate operation calling convention. A generated `to_kwargs()` helper
has been discussed but is **not implemented**.

Do not use `dataclasses.asdict()` as a general migration helper: it recursively
converts nested models, copies values, and does not account for operation
parameter names that differ from model fields.

The current implementation uses one calling convention during developer preview.
Supporting both forms on the same method would require overloads, presence
tracking, and rules for calls that mix an input object with member keywords.
Avoiding that complexity is the reason for the compatibility break, not a claim
that input-object APIs are inherently un-Pythonic. There is no feature flag or
runtime compatibility adapter. Client construction and lifetime are unchanged.

## SDK controls and name collisions

`plugins` is an SDK control, not a modeled input member. It stays keyword-only
and modifies configuration only for the current invocation. A modeled member
named `plugins` receives a trailing underscore, or additional underscores if
that name is already occupied. The input dataclass's field is not renamed.

The current allocator also reserves `self`, pipeline locals such as `input`,
`config`, and `call`, and runtime helpers such as `deepcopy` and imported
operation-plugin functions. It checks all modeled names before choosing an
escape, so `plugins`, `plugins_`, and `plugins__` remain distinct parameters.
Declarations, input construction, docstrings, and generated protocol-test calls
use the same mapping.

Reviewing internal-name reservations and escape stability when new model members
are added remains follow-up work before release.

## Defaults, documentation, and streaming

Parameter types, requiredness, and defaults follow the existing input dataclass
policy (`PythonSymbolProvider`, `CodegenUtils.isRequiredMember`, and
`NullableIndex`), not just Smithy's `@required` trait:

* Required members without defaults become required keyword-only parameters.
* Nullable members without defaults keep `T | None = None`.
* Immutable defaults retain their modeled values.
* Factory-backed defaults (lists, maps, documents) use a private `_Default.UNSET`
  sentinel and create a fresh value per call. Explicit `None`, `0`, `False`, and
  empty strings are not mistaken for omission.

For example, STS `role_arn` and `role_session_name` remain `str | None = None`,
matching the existing input model even though AWS requires these values.
Flattening does not add validation or change that policy.

Operation docstrings retain the operation description, add each member's Smithy
documentation under `Args`, and document `plugins` separately. Types and defaults
appear in the signature. Full member descriptions are retained, so operations
with extensive model documentation can have long docstrings.

Streaming blobs remain typed member parameters and are not eagerly read.
Streaming **union** members remain excluded, as they are from input dataclass
properties. Initial request members are keywords; events use the existing
send/receive workflow. Input-only, output-only, and duplex event-stream return
types are unchanged.

## Generator implementation

* `MemberDefault` shares default rendering between `StructureGenerator` and
  `OperationInputGenerator`. Dataclass-specific behavior stays in
  `StructureGenerator`.
* `OperationInputGenerator` owns parameter names, signatures, member docs,
  construction of the input model, and shallow forwarding from existing models
  in generated call sites.
* `ClientGenerator` uses that rendering for ordinary and event-stream methods.
  It constructs the input after the docstring and before pipeline setup.
* `HttpProtocolTestGenerator` emits keyword calls using model field accesses,
  without recursively converting the input.

The Java generator remains authoritative. These public API rules also apply
when the Python-native generator implements clients; this change does not add a
second generator implementation.

## Verification

With JDK 17 and Pandoc 3.8.2 (the version pinned by CI) installed, run from the
repository root:

```console
uv sync --all-packages --all-extras
source .venv/bin/activate
codegen/gradlew -p codegen :core:test
make test-protocols
```

The existing `PythonCodegenTest` generates the
[Weather fixture](../../codegen/core/src/it/resources/META-INF/smithy/main.smithy)
and checks keyword-only signatures, member documentation, empty inputs, streaming
union exclusion, mutable-default initialization, and collision forwarding in
the generated source. These are source assertions, not Python execution tests.

The existing protocol suites execute generated clients using the new keyword
calls against mock transports. No additional test service, Python subprocess
runner, or CI configuration is needed. These tests do not make live AWS calls
or establish real network streams.

For broader verification, run `make build-java` and `make test-py` with the
virtual environment on `PATH`.

## Downstream rollout

Regenerate `aws/aws-sdk-python/clients`, starting with STS plus a rich
nested/streaming service such as S3. Update the root STS quick-start, service
examples/tests, and the `aws-credentials-sts` resolver's object-style
`assume_role` call. Audit other consumers, waiters, and pagination helpers for
object-style calls; there is no paginator generator in this checkout to update.

Publish the signature change and migration instructions in the same preview
release as the regenerated clients. Run the AWS repository's tests and type
checks after regeneration. Downstream-repository changes and publishing are
separate follow-up work.
