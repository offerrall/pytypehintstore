# Campaña de estrés sobre pytypehintstore 0.0.1

Cinco frentes en paralelo contra el vocabulario completo del core: generación
sistemática de schemas, uniones, valores extremos, profundidad y escala, y el
archivo hostil. El objetivo no era mejorar nada: era romper la librería, y dejar
cada intento como test permanente.

## Números

| | antes | después |
|---|---:|---:|
| tests | 200 | **1577** (+19 xfail estrictos) |
| tiempo de suite | 6,9 s | **29 s** |
| archivos de test | 7 | 12 |
| mypy | limpio | limpio |

Los **200 tests originales siguen en verde**, corridos por separado: la campaña
no dejó una sola regresión.

Schemas generados: **1150**, ninguno rechazado por el core al compilar — el
generador calcula la identidad `(pytype, option_id)` de cada opción antes de
escribir la unión, así que nunca le ofrece al core algo que fuese a refusar.
Cobertura sobre 500 de ellos: profundidad 5, uniones de hasta 5 ramas, 2703
dataclasses anidados, 2122 enums (749 con alias), 3425 opciones `None`, 1394
listas de uniones, 3633 `Min` / 3609 `Max` / 2545 `Choices` / 495 `Pattern`.

---

## El candidato nº 1, resuelto: `inf` / `-inf` / `nan`

**No hay agujero.** El core los rechaza en todas las puertas
(`SchemaValueError: not finite: inf`), así que:

- `add()` no los acepta, y el rechazo ocurre **antes de gastar un id**.
- Como default ni siquiera compilan: `struct_of` lanza `default: not finite`.
- El archivo nunca ve un no-finito, luego `json.dumps` nunca escribe `Infinity`
  ni `NaN` y **el archivo sigue siendo JSON estándar**, legible por cualquier
  lector estricto (verificado con `parse_constant` que lanza).
- P2 no puede romperse por esta vía: el store no puede escribir un archivo que
  luego no sepa releer.
- `nan != nan` habría hecho fallar P1 por definición de igualdad y no por
  corrupción; como el core corta antes, la distinción queda documentada en un
  test y nada más.

La única puerta que queda es un archivo editado a mano, y ahí el store se niega
a abrir entero: `StoreLoadError: row 1: value: not finite: inf`.

---

## Cubo (a) — bugs de pérdida o corrupción

### 1. ARREGLADO · `fingerprint` era exponencial, y `store_of` se colgaba

El core compila cada clase una vez y **comparte** ese objeto entre todos los
campos que la nombran: un schema es un grafo. `_plain` lo recorría como un
árbol, pagando un camino por rama.

```python
# cada nivel alcanzable por dos caminos: una unión y una lista
L(n) = make_dataclass(f"L{n}", [("child", Optional[L(n-1)]),
                                ("items", list[L(n-1)])])
```

| niveles | `struct_of` | `fingerprint` antes | después |
|---:|---:|---:|---:|
| 14 | 0,9 ms | 1,19 s | 0,0005 s |
| 20 | ms | ~10 min | 0,0007 s |
| 40 | ms | horas | 0,0017 s |

Gravedad máxima: `Store.__init__` calcula el fingerprint **antes** de `acquire`,
así que abrir un store sobre una clase que el core compila en un milisegundo se
quedaba colgado **sin error y sin salida** — el único modo de fallo que la regla
de oro prohíbe.

Arreglo (`fingerprint.py`): cada dataclass se escribe una vez y las apariciones
posteriores son una referencia. **El hash fijado no se mueve** (`9d63f00a` sigue
siendo `9d63f00a`), los ciclos siguen cortados y la sensibilidad a un límite
cambiado se conserva.

> **Cambio declarado**: un schema con la misma clase anidada en **dos campos
> distintos** sí cambia de huella, porque su segunda aparición pasa a ser una
> referencia. Renombra el archivo de esas bases de datos. La 0.0.1 no está
> publicada, así que no hay nada ahí fuera que romper.

### 2. ARREGLADO · un surrogate aislado se aceptaba y congelaba todos los volcados

```python
store.add(Text(text="\ud800"))   # aceptado, id 2
```

El core lo valida como el `str` que es, y el round-trip encode→decode→build no
toca UTF-8. Solo la escritura falla — y entonces el hilo escritor reintenta en
silencio para siempre:

```
en memoria: 3 filas | en disco: 1     ← las filas SANAS posteriores no llegan nunca
close(): UnicodeEncodeError            ← tardío, y de otra librería
```

Una sola fila envenenada congelaba el archivo entero, porque un volcado es todo
o nada. Arreglo (`store.py`, `_accepted`): el store hace la última pregunta él
mismo, **después** de que el core haya hablado, así que el error del core sigue
saliendo primero y una fila que el archivo no puede llevar no llega a ser fila:

```
SchemaValueError: cannot be written as UTF-8: 'utf-8' codec can't encode
character '\ud800' in position 10: surrogates not allowed
```

El id no se gasta, y el emoji y cualquier otro UTF-8 legítimo siguen pasando.

### 3. SIN ARREGLAR — decisión tuya · colisión de `option_id` entre un enum y un escalar

**Encontrado por dos frentes de forma independiente.** `EnumShape.option_id()`
es el nombre pelado de la clase, y vive en el mismo espacio que `"str"`,
`"date"` y `"time"` dentro del `$type` que emite el codec. Si una clase enum se
llama `date` —legítimo en Python—, el `$type` colisiona y gana la primera opción
de la unión:

```python
class str(Enum):        # una clase enum honestamente llamada `str`
    LOW = "low"

@dataclass
class Row:
    v: builtins.str | str      # el core compila esto sin una queja

store.add(Row(v=str.LOW))
store.get(1).v                 # 'LOW'  ← un str pelado, no el miembro
```

P1 roto **en el propio `add`**, sin tocar el disco. Con `date | EnumNamedDate` y
su inverso, entra un `date` y sale un miembro, y viceversa.

El core lo permite: `duplicate_options` desempata por `(pytype, option_id)` y los
tipos difieren, y `_check_discriminators` guarda Struct‑vs‑Struct y Enum‑vs‑Enum
en espacios separados, justificándolo con un comentario que dice que los enums
nunca llegan como dict y sus nombres nunca colisionan en el wire — que es
exactamente la suposición que el codec rompe al ponerlos bajo `$type`.

**Por qué no lo he arreglado**: el arreglo puede caer en dos sitios y ninguno es
local ni obvio.

| dónde | qué implica |
|---|---|
| **Core** | Extender `_check_discriminators` a todo el espacio de `option_id`. Es el sitio correcto conceptualmente, pero es otra librería y otro release. |
| **Codec** | Namespacing del `$type` (`"enum:Colour"` vs `"str"`). Correcto y local, pero **cambia el formato del archivo**: los ya escritos con wrapper dejarían de leerse. |
| **Store** (mi recomendación) | Rechazar ruidosamente al abrir un schema cuyas opciones compartan `option_id` en un mismo campo. No cambia el formato, no toca el core, y convierte una corrupción silenciosa en un error al arrancar. ~15 líneas. |

Cobertura: 6 tests `xfail(strict=True)`. Cuando se arregle, pasan a XPASS y
avisan solos.

### 4. SIN ARREGLAR — ya declarado · unión de listas enrutada solo por el tipo de los items

`_branch_of`/`_accepts` leen el tipo de cada item y nada más: ni el `Min`/`Max`
de longitud de la lista, ni `Pattern`/`Choices`/`MultipleOf` sobre los items. El
router del core (`validation.value_branch`) lee la shape entera y acierta. El
codec escribe entonces un `$type` que nombra una rama que rechaza el valor:

```python
f: Annotated[list[str], Min(1)] | list[int]     # con f=[]
# el core rutea a list[int]; el archivo dice list[str]
# add() lanza: too few items: 0, minimum 1
```

Es el límite que la auditoría anterior ya declaró, ahora con caso mínimo y
medida: **4 de 500 schemas generados** lo tocan (seeds 14, 48, 230, 374).

**Lo importante: es ruidoso, nunca silencioso.** El frente de generación buscó
una versión silenciosa de forma exhaustiva —cada par admisible de ramas de lista
× cada valor que el core acepta— y no encontró ninguna: una restricción no puede
cambiar el tipo Python al que decodifica un item, así que una rama mal nombrada
o rechaza la fila o produce el mismo valor. Ese barrido quedó como test
permanente que gritará si eso deja de ser cierto.

Arreglarlo exige el validador de valores del core, que es privado. 10 tests
`xfail(strict=True)`.

---

## Cubo (b) — comportamiento correcto pero no obvio

Arreglados dos, por ser desviaciones del principio declarado ("lo ambiguo viaja
intacto para que lo rechace el core"):

- **ARREGLADO · una clave junto al wrapper se descartaba en silencio.**
  `{"$type": …, "$value": …, "junk": …}` abría, y el siguiente volcado borraba la
  edición a mano sin decir nada. Ahora el dict viaja intacto y el core dice
  `unexpected key(s): junk`.
- **ARREGLADO · un wrapper anidado dentro de otro se desenvolvía.** Ahora se
  refusa: la carga del wrapper es dato, y un wrapper ahí es un archivo roto.
- **ARREGLADO · `"v"` no tenía guarda de tipo.** `payload.get("v") != 1` es una
  igualdad, así que `{"v": 1.0}` cargaba como versión 1 — y `{"v": true}`
  también, porque `True == 1`. Ahora pregunta el tipo primero, como ya hacía
  `next_id`.

Lo demás se queda como documentación ejecutable:

| comportamiento | por qué se queda |
|---|---|
| Claves JSON duplicadas (`{"1": A, "1": B}`): gana la última | `json.loads` decide antes de que el store vea nada. Sin `object_pairs_hook` o un parser propio no hay defensa. |
| `"2026-W01-1"` carga como 2025‑12‑29 y el archivo se reescribe en forma extendida | Contrato de `date.fromisoformat` en 3.11+, no del store. |
| Una copia de rotación con sello escrito a mano (`<stem>.<2**63>.json`) gana el prune para siempre | Las copias son opacas por diseño; su nombre no es una entrada de confianza. |
| Microsegundos en `time` → `SchemaValueError: time precision is limited to whole seconds` | El transporte los llevaría; el core corta antes. |
| `Flag` → `TypeError: Flag enums are not supported` al compilar | Rechazo ruidoso del core. |
| BOM UTF‑8 → `not valid JSON: Unexpected UTF-8 BOM (decode using utf-8-sig)` | El mensaje nombra la cura. |
| Un archivo válido de **otra clase** → `unexpected key(s): …` fila a fila | Orienta por las claves, pero nunca dice "esto es la base de datos de otra clase". |
| Lockfile con el PID del propio proceso → `StoreLockedError` acusándonos | Correcto, y ahora por escrito. |
| `.part` huérfano y copia rotada corrupta | Invisibles para la carga; el siguiente volcado los pisa. |
| Un dataclass de **0 campos** compila y hace round-trip | Corrige el brief, que suponía que el core lo rechazaba. |
| La notación (`Label`, …) no llega al transporte pero **sí** mueve el fingerprint | Es el contrato "todo cuenta, sin letra pequeña", ahora medido por los dos lados. |

### Techos de recursión, medidos

| qué | tope |
|---|---:|
| `struct_of` del core, cadena lineal | **192 niveles** |
| `fingerprint` sobre la misma cadena | **160 niveles** |
| `add` de un `Node` profundo (encode+decode+build) | **159 niveles de datos** |

Hay una banda de ~32 niveles (161–192) donde la clase es un contrato válido y
**ningún store puede abrirse sobre ella**. El fallo es ruidoso (`RecursionError`
saliendo de `store_of`) y **no deja residuos**: ni `.json`, ni `.lock`, nada —
porque el fingerprint corre antes de tomar el lock. Sin corrupción en ningún
punto.

### Tiempos, como dato

Windows 11, Python 3.13.9:

- **5000 filas** de un dataclass de 8 campos con anidados: add 0,58 s (0,12 ms
  por fila) · close 0,13 s · reabrir y reconstruir 0,51 s · archivo 1,74 MB.
- **30 stores de 30 clases en un directorio**, 5 rondas de volcado: 0,66–0,87 s,
  cada familia con sus propias copias y ninguna podando las ajenas.
- **4 hilos, 2 s, `debounce=0.05`**: ~1650 filas netas y **un solo volcado** en
  toda la carrera — el "debounce sin techo" del README, convertido en número.
- **4 hilos, 1 s, `debounce=0`**: ~700 filas, 110 volcados, todos JSON entero.
- En ambos casos el archivo reabierto es **exactamente** `store.all()`: ni una
  fila de más ni de menos, y `next_id` por encima del id más alto.

---

## Cubo (c) — imposibles de construir

- **Dos dataclasses con el mismo `__name__` en una unión**: `struct_of` lanza
  `duplicate discriminator name(s)`, y `list[P] | list[Q]` lanza `duplicate
  option types in shape`. Rechazos ruidosos, con test.
- **Un miembro de enum llamado `"$type"` o `"2026-01-15"` sí es construible**,
  contra lo que suponía el brief: la API funcional de Enum no exige que el
  nombre de miembro sea identificador (solo la sentencia `class`). Y viaja bien:
  llega como **valor**, nunca como clave, así que no puede confundirse con el
  wrapper.

---

## Qué quedó sin cubrir, a sabiendas

- **POSIX.** Todo se midió en Windows 11. La carrera de los dos dueños del
  lockfile está cerrada aquí porque un archivo abierto no se puede borrar; en
  POSIX sí, y eso sigue siendo una duda abierta de la auditoría anterior.
- **Corrupción a nivel de bytes** (un disco que devuelve basura, un truncado a
  media escritura por fallo de hardware). Se probó el kill duro, que es lo que
  `os.replace` cubre por diseño.
- **Concurrencia entre procesos**: fuera del contrato por definición; lo que se
  probó es que el lock la convierte en un error al arrancar.
- **El `option_id` colisionado dentro de un `list[...]`**: los tests cubren la
  unión directa; la variante dentro de una lista es el mismo defecto por el
  mismo camino.

---

## Veredicto

**"Si el core lo compila, el store lo persiste sin pérdida o falla
ruidosamente" se sostiene, con una excepción exacta.**

La excepción es la colisión de `option_id` entre un enum y un escalar de su
mismo grupo de wire (§3): ahí el store acepta y corrompe en silencio. Es el
único caso encontrado en toda la campaña — 1150 schemas generados, 1500
round-trips de codec, 500 comprobaciones de enrutado, y cuatro frentes
adversarios — en que la librería devuelve un valor que no es el que se le dio.
Requiere llamar `date`, `str` o `time` a una clase enum propia.

Todo lo demás que puede salir mal, sale mal a gritos: el core corta los flotantes
no finitos, el store rechaza ahora lo que su archivo no puede llevar, la
recursión da `RecursionError` sin dejar residuos, el enrutado de listas por
restricción falla en `add` y nunca en silencio, y ningún archivo hostil de los
55 probados consigue que el store abra y sirva una fila distinta de la que el
archivo decía.

Los dos bugs con más potencial de daño —el cuelgue silencioso del fingerprint y
el surrogate que congelaba los volcados— eran nuestros, no del core, y están
arreglados con el test que los demuestra por delante.
