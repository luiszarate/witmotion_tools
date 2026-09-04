# WTVB01-BT50 · visualizador para macOS

Interfaz y visualizador en tiempo real para el sensor de vibración
**WitMotion WTVB01-BT50** conectado por puerto serial (cable USB-C).

- Backend en Python: lectura serial, decodificación de tramas, grabación CSV.
- Interfaz web local (se abre sola en el navegador): tarjetas numéricas,
  gráficas de tiempo real y tabla de registros en crudo.
- Única dependencia: `pyserial`. El resto es librería estándar.

---

## Uso rápido

```bash
./run.sh
```

Crea el entorno virtual la primera vez, arranca el servidor en
`http://127.0.0.1:8787/` y abre el navegador. Elige el puerto (`/dev/cu.*`)
y pulsa **Conectar**.

Otros comandos:

```bash
./run.sh ports            # lista los puertos serie candidatos
./run.sh modes            # describe los modos de captura
./run.sh monitor          # lectura en el terminal, sin interfaz
./run.sh record -d 60     # graba 60 s a CSV y termina
./run.sh serve --connect  # arranca ya conectado al primer puerto probable
./run.sh monitor -m high_speed --poll-interval 0.02
```

Opciones comunes: `-p/--port`, `-b/--baudrate` (115200), `-m/--mode`,
`--poll-interval`, `--frame-length`. `./run.sh --help` y
`./run.sh <comando> --help`.

Los CSV se guardan en `~/Documents/wtvb01-logs/` con una columna por canal.

---

## Modos de captura

Se cambian **desde la interfaz sin desconectar** (selector *Modo* en la barra
superior) o con `-m` en la línea de comandos. El sensor no tiene registro de
modo: lo que su software de PC llama modo normal y modo de alta velocidad es
una decisión del **host** sobre qué bloques de registros leer y con qué
frecuencia. Cambiar de modo no escribe nada en el sensor.

| Modo | Qué sondea | Por defecto | Para qué |
|------|------------|-------------|----------|
| **Normal** | Alterna los bloques `0x3A` y `0x42` | 2 Hz (cada bloque a 1 Hz) | Todos los canales de vibración. El modo de trabajo habitual. |
| **Alta velocidad** | 9 lecturas de `0x42` por cada una de `0x3A` | 50 Hz | Forma de onda de desplazamiento (`0x47`–`0x49`), sin perder temperatura ni desplazamiento X. |
| **Solo stream** | Nada | – | Solo la trama continua a 100 Hz: aceleración, giro, velocidad y ángulo. Carga mínima del bus. |

La trama continua `0x61` llega siempre, en los tres modos.

**Por qué el reparto desigual en alta velocidad:** una lectura del bloque
`0x42` devuelve los registros `0x42`–`0x49` (desplazamiento Y/Z, frecuencia y
la onda de alta velocidad), y una del bloque `0x3A` devuelve `0x3A`–`0x41`
(velocidad, ángulo, temperatura y desplazamiento X). Dándole nueve de cada
diez lecturas al primero, la onda sale a ~45 Hz y el resto sigue vivo a ~5 Hz.
Medido a 50 Hz durante 3 s: 133 de 133 respuestas del bloque de
desplazamiento y 14 de 15 del bloque de vibración.

**Techo de sondeo:** medido en una unidad física con la trama de 100 Hz
corriendo, una lectura cada 20 ms obtuvo 97 de 100 respuestas; cada 10 ms,
solo 25 de 200. De ahí el suelo de 10 ms, que la interfaz aplica sola.

La interfaz marca cada gráfica según lo que el modo activo le haga a ese canal:

- sin etiqueta — se refresca a un ritmo útil;
- **submuestreado** — se refresca, pero mucho más lento que la señal que
  representa (la onda de alta velocidad en modo normal);
- **congelado** — el modo no lo refresca; se muestra el último valor conocido.

---

## Conexión en macOS

El sensor usa un puente USB-serie CH340 (`VID:PID=1A86:7523`). macOS 11+ trae
el driver incluido, así que aparece solo como `/dev/cu.usbserial-XXX` al
conectar el cable USB-C. **Siempre `/dev/cu.*`, nunca `/dev/tty.*`**: el
segundo se bloquea al abrir esperando la señal de carrier.

Parámetros: 115200 baudios, 8N1.

> El enlace Bluetooth 5.0 del sensor **no** crea un puerto serie en macOS (es
> BLE, no SPP). Para BLE haría falta un backend aparte con `bleak`; el
> decodificador ya está preparado para ello (ver `LAYOUT_BLE`).

---

## Protocolo

Todo lo que sigue está verificado contra una unidad física, no solo contra el
manual. Ver `wtvb01/protocol.py` y `tests/test_protocol.py`.

### Tramas

Cada trama empieza con `0x55` seguida de un byte de tipo. **No hay checksum**,
así que el parser se resincroniza por byte de sincronía y valida por longitud.

| Tipo | Contenido | Longitud |
|------|-----------|----------|
| `0x61` | Salida continua: enteros `int16` little-endian | 28 / 32 / 40 bytes según transporte |
| `0x71` | Respuesta a lectura de registros: dirección + 8 registros | 20 bytes |

La longitud de la trama `0x61` **cambia según el transporte y el firmware** y
no viene indicada en la trama. El programa la detecta sola midiendo la
distancia entre pares `55 61` consecutivos (`wtvb01/detect.py`); se puede fijar
con `--frame-length`.

| Longitud | Nombre | Registros que trae |
|----------|--------|--------------------|
| 28 | `manual-28` | `0x3A`–`0x46` (el formato del manual) |
| 32 | `ble-32` | `0x3A`–`0x46` + contador de energía (`0x64`) |
| 40 | `uart-40` | `0x30`–`0x3F`: marca de tiempo, acelerómetro, giróscopo, velocidad y ángulo |

### Lo importante del formato `uart-40`

Esta es la trama que emite la unidad probada por el puerto USB-C, y **no trae
temperatura, desplazamiento ni frecuencia**: las ranuras donde caerían los
registros `0x40`–`0x42` leen siempre cero, mientras que una lectura explícita
de esos mismos registros devuelve valores vivos (`0x40` → 3427 = 34.27 °C).

Por eso el lector **consulta los bloques de medición por separado**
(`FF AA 27 3A 00` y `FF AA 27 42 00`, alternados) cada `--poll-interval`
segundos, y acumula: la trama continua refresca aceleración/velocidad/ángulo a
100 Hz y el sondeo rellena temperatura, desplazamiento y frecuencia a ~1 Hz.
Con `--poll-interval 0` se desactiva el sondeo.

El sensor solo responde al **primer** comando de lectura de un par consecutivo,
de ahí que los bloques se pidan por turnos y no juntos.

### Registros

| Dirección | Canal | Unidad | Conversión |
|-----------|-------|--------|------------|
| `0x03` | Return rate | código | 0x09 = 100 Hz |
| `0x34`–`0x36` | Aceleración X/Y/Z | g | `raw / 32768 * 16` |
| `0x37`–`0x39` | Giróscopo X/Y/Z | °/s | `raw / 32768 * 2000` |
| `0x3A`–`0x3C` | Velocidad de vibración X/Y/Z | mm/s | `raw` |
| `0x3D`–`0x3F` | Amplitud angular X/Y/Z | ° | `raw / 32768 * 180` |
| `0x40` | Temperatura del módulo | °C | `raw / 100` |
| `0x41`–`0x43` | Desplazamiento X/Y/Z | µm | `raw` |
| `0x44`–`0x46` | Frecuencia X/Y/Z | Hz | `raw` |
| `0x47`–`0x49` | Desplazamiento alta velocidad | µm | `raw` |
| `0x5D`/`0x5E` | Frecuencia de corte (entero/decimal) | Hz | `raw` |
| `0x5F` | Ciclo de detección | Hz | `raw` |
| `0x64` | Contador de energía | – | sin documentar; se observan valores fuera de 0–100 |

La temperatura es la **del módulo**, no la del rodamiento ni la de la máquina:
mide entre la superficie donde va montado y el aire, y va con retraso.

### Comandos

| Acción | Bytes |
|--------|-------|
| Leer 8 registros desde `XX` | `FF AA 27 XX 00` |
| Escribir registro | `FF AA <reg> <lo> <hi>` |
| Desbloquear config (10 s) | `FF AA 69 88 B5` |
| Guardar | `FF AA 00 00 00` |

---

## Estructura

```
wtvb01/
  registers.py   mapa de registros, unidades y escalas
  modes.py       modos de captura: qué bloques sondear y cada cuánto
  protocol.py    tramas, layouts, parser resincronizable, comandos
  detect.py      autodetección de la longitud de trama 0x61
  model.py       Vector3, Sample, Accumulator (todo inmutable)
  ports.py       descubrimiento de puertos serie en macOS
  source.py      hilo lector: serial -> tramas -> muestras
  hub.py         historial + reparto a suscriptores, con límite de tasa
  recorder.py    escritura CSV
  app.py         Session: conexión, modo, grabación, escritura de configuración
  server.py      HTTP + Server-Sent Events (solo 127.0.0.1)
  cli.py         serve / monitor / record / ports / modes
  web/           interfaz (HTML + CSS + JS sin dependencias)
tests/           125 pruebas, sin hardware
```

## Pruebas

```bash
python3 -m unittest discover -s tests -t .
```

Ver [tests/README.md](tests/README.md) para cobertura.

## API HTTP

Todo en `127.0.0.1`, sin autenticación, pensado como interfaz de la propia
aplicación.

| Método | Ruta | Qué hace |
|--------|------|----------|
| GET | `/api/ports` | Puertos serie candidatos |
| GET | `/api/status` | Estado de conexión, modo, tasas, última muestra |
| GET | `/api/registers` | Catálogo de registros, modos y tasas de retorno |
| GET | `/api/history` | Últimas muestras en memoria |
| GET | `/api/stream` | Server-Sent Events con cada muestra |
| POST | `/api/connect` | `{port, mode, poll_interval, baudrate, output_length}` |
| POST | `/api/disconnect` | Cierra el puerto |
| POST | `/api/mode` | `{mode, poll_interval}` — cambia el modo en caliente |
| POST | `/api/record` | `{action: "start"｜"stop", directory}` |
| POST | `/api/setting` | `{name, value}` — escribe un registro de configuración |

## Configuración del sensor

El panel *Configuración del sensor* escribe en el sensor (frecuencia de corte,
ciclo de detección, tasa de retorno). A diferencia del modo de captura, esto
**sí** modifica el dispositivo de forma persistente: la secuencia es
desbloquear (`FF AA 69 88 B5`), escribir y guardar (`FF AA 00 00 00`). La
interfaz pide confirmación en cada escritura.

## Estado

Funcionan la visualización, los tres modos de captura conmutables en caliente,
la escritura de configuración y la grabación. Siguientes pasos naturales: FFT
del desplazamiento capturado en alta velocidad, umbrales ISO 10816 sobre la
velocidad de vibración y un backend BLE con `bleak`.

## Pendiente: conexión por Bluetooth

El sensor es BLE 5.0 y el enlace es viable, pero **está sin verificar contra
hardware**. Lo investigado hasta ahora, para no repetirlo:

**Protocolo.** Esquema estándar de WitMotion BLE: servicio
`0000ffe5-0000-1000-8000-00805f9b34fb`, notificaciones en `0000ffe4-…`,
escritura en `0000ffe9-…`. Las tramas son las mismas que por serie, así que
`protocol.py` ya sirve tal cual: `LAYOUT_BLE` cubre la trama de 32 bytes que
emite este modelo por BLE, y la autodetección de longitud la elegiría sola.
Solo falta un `BleSource` equivalente a `SerialSource` (con `bleak`, ya
instalado en el venv aunque no esté en `requirements.txt`).

**El bloqueo real es de permisos de macOS, no del sensor.** Todo intento de
escanear muere con `SIGABRT`:

> `TCC: This app has crashed because it attempted to access privacy-sensitive
> data without a usage description. The app's Info.plist must contain an
> NSBluetoothAlwaysUsageDescription key.`

Comprobado:

- El `Python.app` de Homebrew (ad-hoc, `org.python.python`) no lleva esa clave.
- Construir un bundle propio **con** la clave, sellada en la firma, tampoco
  basta: TCC atribuye la petición al *proceso responsable*, no al que la hace.
- `Terminal.app` no declara la clave. `Claude.app` sí, pero el bundle anidado
  `com.anthropic.claude-code`, que es quien lanza la shell, no.

Así que hay que ejecutarlo desde una app que sí tenga permiso de Bluetooth
concedido, o parchear el `Info.plist` de `Python.app` y volver a firmarlo
(se pierde en cada `brew upgrade`).

Nada de esto afecta al enlace por USB-C, que funciona.

## Fuentes

- [Manual WTVB01-BT50 (manuals.plus)](https://manuals.plus/m/363e32baa8bfd617ed694548364d59284eb4144afb9cfda9a01f4615a3be5efb)
- [WITMOTION/WitBluetooth_BWT901BLE5_0](https://github.com/WITMOTION/WitBluetooth_BWT901BLE5_0) — SDK oficial con ejemplo para WTVB01-BT50
- [NatanBack77/zenith-edge-collector](https://github.com/NatanBack77/zenith-edge-collector) — protocolo BLE verificado contra hardware
