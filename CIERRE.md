# Cierre de 0.0.2

Temporal, para revisión humana. No forma parte del repo publicable.

```
$ python -m pytest -q
1583 passed, 13 xfailed in 29.44s

$ python -m mypy            # src
Success: no issues found in 6 source files
$ python -m mypy tests
Success: no issues found in 14 source files
```

| | 0.0.1 | 0.0.2 |
|---|---:|---:|
| tests | 200 | **1583** (+13 xfail) |
| tiempo de suite en frío | 6,9 s | **29,4 s** (presupuesto 60 s) |
| líneas de `src/` | 861 | **928** |
| módulos | 5 | 6 |

---

## Fase 1 — el arreglo del enum, completo

### `src/pytypehintstore/store.py`

Importa `List` y `Struct`, ya públicos y ya usados por el codec:

```diff
-from pytypehint import SchemaTypeError, SchemaValueError, struct_of
+from pytypehint import List, SchemaTypeError, SchemaValueError, Struct, struct_of
```

La guarda corre en `__init__`, después de derivar el nombre del archivo (para
poder citarlo) y **antes** de `folder.mkdir` y de `acquire`:

```diff
         self._path = folder / name
         self._name = str(Path(directory) / name)
+        self._ambiguous()
         self._debounce = float(debounce)
```

El método, junto a `_open`:

```python
# Two options of one union that answer to the same transport name are
# indistinguishable in the file: the wrapper names an option and the reader
# takes the first that answers. The core allows the pair — an enum class
# called `date` competes with `date` itself, in different namespaces to it —
# so the store refuses at the door rather than routing a value to the wrong
# branch and handing back something nobody stored.
def _ambiguous(self) -> None:
    found = _shared_name((self._schema,), (), set())

    if found is None:
        return

    path, shared = found
    where = ": ".join(path)
    raise StoreError(
        f"{self._name}: {where}: two options of a union share the transport "
        f"name {shared!r}: rename one of the classes")
```

Y el recorrido, a nivel de módulo (18 líneas):

```python
# The first pair of options sharing a transport name, as (path, name), or None.
# `seen` keeps a recursive schema from being walked forever.
def _shared_name(shapes, path, seen):
    names = set()

    for shape in shapes:
        # A Struct carries its $type inside the object and never competes for
        # the wrapper's, so a dataclass and an enum may share one name — the
        # core allows exactly that, and the codec keeps them apart.
        if type(shape) is Struct:
            continue

        name = shape.option_id()

        if name in names:
            return path, name

        names.add(name)

    for shape in shapes:
        if type(shape) is Struct:
            if id(shape) in seen:
                continue

            seen.add(id(shape))

            for field in shape.fields:
                found = _shared_name(field.shape, (*path, field.name), seen)

                if found is not None:
                    return found

        elif type(shape) is List:
            found = _shared_name(shape.item, path, seen)

            if found is not None:
                return found

    return None
```

**La exclusión de `Struct` no estaba en el prompt y es necesaria.** Sin ella la
guarda se pasaba de estricta y rompía un test verde de la suite anterior
(`test_codec.py::test_an_enum_sharing_its_name_with_a_dataclass_survives_as_a_member`):
un dataclass y un enum homónimos **funcionan correctamente**, porque un `Struct`
lleva su `$type` dentro del objeto y nunca compite por el del wrapper — es
exactamente lo que arregló la auditoría anterior. La guarda mira solo a las
opciones que pueden viajar envueltas.

Cero privados del core: `option_id()`, `fields`, `field.shape` y `List.item` son
públicos, y `tests/test_boundary.py` sigue en verde sobre los seis módulos.

### Comportamiento, verificado a mano

| caso | resultado |
|---|---|
| `date \| EnumNamedDate` | `RECHAZA — when: two options of a union share the transport name 'date': rename one of the classes` |
| el mismo enum **sin** `date` al lado | **ABRE**, y guarda sus miembros |
| anidado a 3 niveles | `RECHAZA — outer: inner: when: …` (el path completo) |
| dentro de `list[...]` | `RECHAZA — items: …` |
| dos enums homónimos | lo rechaza **el core** al compilar (`duplicate discriminator name(s)`) |
| uniones normales (`str\|date`, `list[str]\|list[int]`, `Enum\|None`) | **ABREN** |

---

## Estado final de los 19 xfail

**Seis eran del enum: ninguno queda.** Convertidos en tests del rechazo:

| antes (xfail) | ahora |
|---|---|
| `unions::test_an_enum_named_str_keeps_its_member_through_the_store` | `test_an_enum_sharing_a_transport_name_with_a_scalar_is_refused_at_open` — mensaje completo por igualdad |
| `unions::test_a_date_survives_an_enum_named_date_with_an_iso_member` | `test_a_date_beside_an_enum_named_date_is_refused_either_way_round[enum first]` |
| `unions::test_an_enum_member_named_like_an_iso_date_survives_a_date_option` | el mismo, `[date first]` |
| `generated::test_enum_named_like_a_scalar_loses_the_member` (codec puro) | `test_an_enum_named_like_a_scalar_is_why_the_store_checks_the_names` — **adaptado, no borrado**: pinta las dos mitades, lo que el codec haría y el rechazo que lo impide, así que quitar la guarda deja un test diciendo qué vuelve en su lugar |
| `generated::test_enum_named_like_a_scalar_corrupts_a_stored_row` | `test_a_schema_the_codec_could_not_name_never_becomes_a_store` |
| `generated::test_enum_named_like_date_loses_the_member` | `test_an_enum_named_like_date_is_refused_the_same_way` |

Además, dos tests que **pasaban** documentando la mitad superviviente de la
colisión (`test_an_enum_named_str_takes_a_plain_string_with_it`,
`test_the_same_collision_refuses_other_values_out_loud`) dejaron de tener
sentido —el store ya no abre ese schema— y se sustituyeron por
`test_a_refused_schema_leaves_nothing_behind` y
`test_an_enum_named_like_a_scalar_it_never_meets_is_fine` (el caso negativo).

**Los 13 restantes son todos el mismo límite declarado**, el de la unión de
listas enrutada por el tipo de los items, ahora con una línea apuntando al
README en su `reason`:

| test | qué fija |
|---|---|
| `generated::test_codec_round_trip[seed14, 48, 230, 374]` | los 4 de 500 schemas generados que lo tocan |
| `generated::test_the_transport_names_the_branch_the_core_would_choose[seed14, 48, 230, 374]` | el `$type` escrito no es la rama que `value_branch` elegiría |
| `generated::test_store_round_trip[seed14, 48]` | los mismos, a través de un store real |
| `generated::test_a_union_of_lists_routes_by_type_alone_on_length` | el caso mínimo: `[]` bajo `Annotated[list[str], Min(1)] \| list[int]` |
| `…_on_a_pattern` | lo mismo con `Pattern` |
| `…_on_choices` | lo mismo con `Choices` |

Ninguno quedó obsoleto por la fase 1: son un defecto distinto, en otra capa.

---

## Fase 2 — limpieza

- **Presupuesto: 29,4 s en frío**, menos de la mitad de los 60 s. Ningún test
  necesitó `@pytest.mark.slow`, así que no se registró el marcador y el README
  no necesita instrucciones aparte. Los cinco más lentos: 2,1 s (4 hilos contra
  el escritor), 1,4 s (bisección del techo de recursión), 1,3 s (5000 filas),
  1,0 s (cobertura del generador), 1,0 s (medición del ritmo de reintento).
- **Código muerto: no había.** Un barrido AST sobre los 14 archivos de test
  buscando funciones y clases definidas y nunca usadas no encontró ninguna.
- **mypy limpio en tests**, además de en src. Cinco avisos, todos por
  construcciones deliberadas: tres `Enum("date", …)` guardados en variables con
  otro nombre (es el bug que se está probando), un `v: str | ShadowStr` con una
  variable como tipo, y una anotación que faltaba en `_LIST_VALUES`. Los cuatro
  primeros llevan `# type: ignore` con el código exacto; el quinto se anotó.
  `warn_unused_ignores` está activo, así que si el motivo desaparece, avisa.

---

## Fase 4 — empaquetado

```
$ python -m build
Successfully built pytypehintstore-0.0.2.tar.gz and
                  pytypehintstore-0.0.2-py3-none-any.whl

$ unzip -l dist/pytypehintstore-0.0.2-py3-none-any.whl
  pytypehintstore/__init__.py  codec.py  errors.py  fingerprint.py
  lockfile.py  store.py  py.typed
  pytypehintstore-0.0.2.dist-info/{METADATA,RECORD,WHEEL,licenses/LICENSE}
```

**El quick start del README, tal cual, contra el wheel en un venv limpio:**

```
$ .venv/Scripts/pip install dist/pytypehintstore-0.0.2-py3-none-any.whl
pytypehint 0.0.7 · pytypehintstore 0.0.2

$ .venv/Scripts/python quickstart.py
path.name: Task.a50534e4.json          ← el mismo nombre que documenta el README
get: Task(title='Buy milk', priority=<Priority.HIGH: 'high'>, done=False)
all: [(2, Task(title='Write the store', priority=<Priority.LOW: 'low'>, done=True))]
reopened: ids kept, next add gave 3
py.typed viaja: True
```

`example.py` contra ese mismo wheel, tres ejecuciones seguidas: ids 1‑2, 3‑4,
5‑6, las filas anteriores intactas y las copias rotadas de su familia. El
directorio acabó con las **dos** bases de datos conviviendo — la `Task` del quick
start (`a50534e4`) y la `Task` del ejemplo (`975cbdd0`), dos clases con el mismo
nombre y distinto contrato — que es el contrato hecho visible.

---

## Decisiones que tomé sin cobertura del prompt

1. **La exclusión de `Struct` en la guarda** (arriba). Sin ella, sobre‑rechazo.
2. **La dependencia se fija en `pytypehint>=0.0.7`**, la versión actual y contra
   la que está probado todo. No bisecté qué versión anterior bastaría: habría
   exigido instalar y correr la suite contra cada una, y el core es del mismo
   autor y avanza junto.
3. **Las URLs del `pyproject` asumen `github.com/offerrall/pytypehintstore`**,
   por el patrón del resto del ecosistema. **Confírmalas antes de publicar**: si
   el repo se llama de otro modo, son tres líneas.
4. **La entrada 0.0.1 del CHANGELOG llevaba dos frases falsas** —la firma vieja
   `store_of(cls, path, …)` y el remedio de `missing key(s)`, ambos retirados en
   el rediseño— y las corregí en vez de dejarlas: esa versión nunca se publicó,
   así que su entrada describe la librería, no un histórico.
5. **`mypy` sigue configurado con `files = ["src"]`**. Los tests pasan si se le
   pide explícitamente (`mypy tests`), pero no los añadí a la configuración: lo
   que se publica es el paquete, y el CI ya corre `mypy src`.
6. **El test del codec puro del enum se adaptó en vez de eliminarse.** El prompt
   dejaba elegir; conservarlo documenta por qué existe la guarda, y falla con un
   mensaje útil si alguien la quita.

---

## Lo que queda fuera, a sabiendas

- **El límite de la unión de listas** sigue abierto. Arreglarlo necesita el
  router de valores del core, que es privado; la alternativa sería reimplementar
  las restricciones en el codec, es decir, el segundo validador que el diseño
  no quiere. Está en el README, en 13 xfail estrictos y en `INFORME.md`.
- ~~**POSIX.** La carrera de los dos dueños del lockfile está cerrada en Windows
  porque un archivo abierto no se puede borrar; en POSIX sí.~~ **Cerrado en
  0.0.4**: POSIX toma un `flock` que el kernel suelta al morir el proceso, más
  una comprobación de inodo. La suite corre ahora en `ubuntu-latest` y
  `windows-latest`. Queda una diferencia declarada, con test por plataforma: un
  lockfile que nadie sostiene lo toma POSIX (el `flock` es la evidencia) y lo
  respeta Windows (el PID lo es).
- **El traceback compartido del segundo `close()`** (cosmético, ya declarado).
- **`INFORME.md`** se queda en el repo con el detalle de la campaña; dime si
  prefieres que se vaya con este `CIERRE.md`.

---

## Veredicto

La propiedad se sostiene con una excepción, y la excepción es ruidosa: **si el
core compila el schema, el store lo persiste sin pérdida o falla a gritos** — en
`add`, o en `store_of` antes de tocar el disco. El único caso de corrupción
silenciosa que la campaña encontró está cerrado, y lo que queda es un rechazo
molesto y raro (4 de 500 schemas), no una pérdida.

Queda listo para `twine upload`. Ese botón es tuyo.
