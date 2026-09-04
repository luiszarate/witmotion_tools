# WTVB01-BT50 · logger para Raspberry Pi

Servicio desatendido que registra uno o varios sensores de vibración
**WitMotion WTVB01-BT50** en archivos CSV con rotación por tiempo. Los
sensores se declaran en un archivo de configuración y se conectan por cable
(USB-serie) o por Bluetooth LE, mezclados si hace falta.

Pensado para **Raspberry Pi Zero 2 W**.

> **Para poner esto en marcha, usa [DESPLIEGUE.md](DESPLIEGUE.md)** — es el
> manual de instalación, configuración, uso diario y resolución de problemas.
> Este README es la referencia técnica: protocolo, arquitectura y hallazgos.

---

## Instalación

```bash
git clone <repo> && cd witmotion_tools/pi && sudo ./install.sh
```

Crea el usuario de servicio `wtvb01`, un entorno virtual en
`/opt/wtvb01-logger`, la configuración en `/etc/wtvb01-logger/config.toml` y
la unidad de systemd. Te añade a ti al grupo `wtvb01` para que puedas mandar
comandos de control sin `sudo` (hay que volver a iniciar sesión).

Después:

```bash
sudoedit /etc/wtvb01-logger/config.toml
```

```bash
sudo systemctl start wtvb01-logger && wtvb01-logger status
```

---

## Descubrir los sensores

```bash
wtvb01-logger ports
```

```bash
wtvb01-logger ble-scan
```

El primero lista los puertos serie (`/dev/ttyUSB0` para el puente CH340 que
lleva este sensor); el segundo, las direcciones BLE. Copia lo que salga al
archivo de configuración.

---

## Configuración

TOML. Ver [config.example.toml](config.example.toml) para el archivo completo
comentado.

```toml
[logger]
output_dir = "/var/lib/wtvb01-logger"
rotate_minutes = 15
flush_seconds = 2
control_socket = "/run/wtvb01-logger/control.sock"

[defaults]
mode = "normal"
reconnect_seconds = 5
max_rate_hz = 0

[[sensors]]
name = "rotor-izq"
transport = "serial"
port = "/dev/ttyUSB0"

[[sensors]]
name = "rotor-der"
transport = "ble"
address = "AA:BB:CC:DD:EE:FF"
mode = "high_speed"
poll_interval = 0.02
max_rate_hz = 50
```

Claves por sensor: `name`, `transport` (`serial`｜`ble`), `port`｜`address`,
`baudrate`, `mode`, `poll_interval`, `reconnect_seconds`, `max_rate_hz`,
`output_length`, `enabled`.

La validación es estricta: **una clave mal escrita es un error, no un
silencio**. En un logger sin pantalla, una opción ignorada no se nota hasta
que faltan los datos.

```bash
wtvb01-logger -c /etc/wtvb01-logger/config.toml validate
```

### `max_rate_hz`, o cómo no llenar la tarjeta

A 100 Hz un sensor escribe unas 100 filas por segundo, del orden de **90 MB
por hora**. `max_rate_hz` diezma antes de escribir: `max_rate_hz = 10` deja
unos 9 MB/h por sensor. `0` registra todo.

---

## Control por SSH

El servicio escucha en un socket Unix. Todos estos comandos funcionan por SSH
sin parar la captura ni soltar el enlace con el sensor:

```bash
ssh pi-logger wtvb01-logger status
```

| Comando | Efecto |
|---------|--------|
| `status` | Estado por sensor: enlace, modo, tasas, archivo actual, filas |
| `roll` | Cierra los archivos actuales y empieza otros nuevos |
| `pause` | Deja de escribir, sin desconectar el sensor |
| `resume` | Vuelve a escribir |
| `stop` | Para el servicio ordenadamente, cerrando los archivos |
| `ping` | Comprueba que el servicio responde |

`roll`, `pause` y `resume` aceptan `--sensor <nombre>` para actuar sobre uno
solo. Todos aceptan `--json`.

`systemctl reload wtvb01-logger` (o `kill -HUP`) también hace un `roll`
completo, por si el socket no está a mano.

---

## Qué se registra

Un directorio por sensor, y dentro archivos `<sensor>-<AAAAMMDD-HHMMSS>.csv`:

```
/var/lib/wtvb01-logger/
├── rotor-izq/
│   ├── rotor-izq-20260904-100000.csv
│   └── rotor-izq-20260904-101500.csv
└── rotor-der/
    └── rotor-der-20260904-100000.csv
```

Columnas: `sensor`, `timestamp_iso`, `t_epoch` y después **un canal por
registro en unidades físicas** — velocidad de vibración (mm/s), amplitud
angular (°), temperatura del módulo (°C), desplazamiento (µm), frecuencia
(Hz), desplazamiento de alta velocidad (µm), aceleración (g) y giro (°/s).

El juego de columnas es fijo, así que archivos de sesiones o sensores
distintos se concatenan sin alinear nada. Una columna vacía significa que ese
canal no llegó en esa muestra — lo cual depende del modo de captura.

### Modos de captura

Los mismos tres del monitor de escritorio, por sensor:

| Modo | Sondea | Por defecto | Canales vivos |
|------|--------|-------------|---------------|
| `normal` | Alterna bloques `0x3A` y `0x42` | 2 Hz | Todos |
| `high_speed` | 9 lecturas de `0x42` por cada una de `0x3A` | 50 Hz | Todos, con la onda de desplazamiento a ~45 Hz |
| `stream` | Nada | – | Solo aceleración, giro, velocidad y ángulo |

Ver el [README del monitor](../mac/README.md) para el detalle del protocolo:
mapa de registros, tramas y por qué el reparto de alta velocidad es 9:1.

---

## Resiliencia

Un sensor ausente es normal, no un fallo. Cada sensor tiene su propio hilo
supervisor: si no conecta —desenchufado, fuera de alcance BLE, aún sin
alimentación— reintenta con backoff creciente hasta seis veces el
`reconnect_seconds` configurado, mientras **los demás sensores siguen
registrando**. Si un enlace ya establecido se cae, se reconecta igual.

La unidad de systemd usa `Restart=always` sin límite de reintentos, y
`TimeoutStopSec=20` para que dé tiempo a cerrar los CSV.

---

## Estructura

```
wtvb01_logger/
  config.py     carga y validación del TOML
  sinks.py      CSV con rotación por tiempo, pausa y diezmado
  worker.py     un sensor supervisado: conectar, registrar, reconectar
  control.py    socket Unix de control (servidor y cliente)
  service.py    orquestación y comandos
  cli.py        run / validate / ports / ble-scan / status / roll / …
tests/          41 pruebas, sin hardware
```

El protocolo, el mapa de registros, los modos y los transportes viven en
[`../core`](../core), compartidos con el monitor de escritorio.

## Pruebas

```bash
python3 -m unittest discover -s tests -t .
```

## Bluetooth: lo aprendido en hardware real

Verificado en una Raspberry Pi Zero 2 W con Debian 13 y BlueZ 5.82, contra un
WTVB01-BT50 físico. Cuatro cosas que no están en ningún manual:

**El adaptador viene bloqueado.** La imagen trae `hci0` con *soft block* de
rfkill, así que BlueZ está corriendo pero la radio apagada y ningún sensor
aparece. La unidad de systemd hace `rfkill unblock bluetooth` al arrancar; a
mano:

```bash
sudo rfkill unblock bluetooth && sudo hciconfig hci0 up
```

**Hay que escanear antes de conectar.** BlueZ no conecta a una dirección
cuyo anuncio no ha visto: un `BleakClient(address)` a secas falla con *device
not found* aunque el sensor esté al lado. El transporte escanea primero y solo
usa la dirección desnuda como respaldo.

**Desconectar bien no es opcional.** Si el proceso muere sin cerrar el enlace
GATT, BlueZ lo mantiene abierto — y un periférico BLE conectado **deja de
anunciarse**, así que ningún escaneo posterior lo encuentra y queda
irrecuperable hasta un `bluetoothctl disconnect`. El cierre espera a que la
desconexión termine antes de bajar el bucle de eventos.

**La trama por BLE es la misma de 40 bytes que por cable.** No la de 32 que
documentan otras fuentes para este modelo. La autodetección de longitud lo
resuelve sola, pero conviene saberlo.

### Rendimiento por BLE

| Enlace | Tasa de trama |
|--------|---------------|
| Cable (USB-serie) | ~100 Hz, estable |
| BLE | **~50-90 Hz, variable** |

Medido con el mismo sensor y sin escribir CSV: 102 Hz, 72 Hz y 58 Hz en
ejecuciones distintas. Cero bytes descartados en todas, así que no se pierden
tramas al decodificar — es el intervalo de conexión que negocia la radio lo
que entrega menos. Si necesitas la forma de onda de aceleración con ancho de
banda garantizado, usa el cable.

La escritura del CSV **no** es el cuello de botella: ocurre en un hilo
aparte, con una cola de 2000 muestras, para que un bloqueo de la tarjeta SD
nunca frene la radio. `status` avisa si esa cola se llena.

## Estado

Verificado en hardware: Raspberry Pi Zero 2 W, Debian 13, Python 3.13, contra
un WTVB01-BT50 real por Bluetooth. Probados el descubrimiento, la conexión, el
sondeo de registros, la escritura de CSV, la rotación por tiempo y todos los
comandos de control (`status`, `roll`, `pause`, `resume`, `stop`) sin soltar
el enlace.

Sin verificar todavía: la instalación como servicio de systemd
(`install.sh`), el registro simultáneo de varios sensores, y el transporte
por cable en la propia Pi (probado en macOS, no en Linux).
