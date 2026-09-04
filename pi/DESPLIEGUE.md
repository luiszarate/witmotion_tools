# Despliegue y operación · logger WTVB01-BT50 en Raspberry Pi

Manual de puesta en marcha y uso diario. Para el detalle técnico del
protocolo y la arquitectura, ver [README.md](README.md).

---

## 1. Qué hace

Un servicio que arranca solo con la Raspberry, se conecta a uno o varios
sensores de vibración WTVB01-BT50 —por cable o por Bluetooth— y escribe sus
lecturas en CSV con rotación por tiempo. Se controla por SSH sin interrumpir
la captura.

---

## 2. Requisitos

Verificado sobre:

| | |
|---|---|
| Placa | Raspberry Pi Zero 2 W Rev 1.0 |
| SO | Debian GNU/Linux 13 (trixie), kernel 6.12 aarch64 |
| Python | 3.13.5 (con `tomllib` en la stdlib) |
| BlueZ | 5.82 |
| Sensores | WitMotion WTVB01-BT50, anunciados como `WTSensor-NN` |

Requisitos mínimos: Python 3.11 o superior (por `tomllib`), systemd, y BlueZ
solo si vas a usar sensores por Bluetooth.

Espacio en disco: ver [§7](#7-cuánto-ocupa).

---

## 3. Instalación

Desde tu máquina, copia el repositorio a la Raspberry:

```bash
rsync -az --exclude '.venv' --exclude '__pycache__' --exclude '.git' ./core ./pi pi-zero:~/witmotion_tools/
```

Y en la Raspberry:

```bash
cd ~/witmotion_tools/pi && sudo ./install.sh
```

El instalador es idempotente —se puede volver a ejecutar para actualizar— y
**nunca sobrescribe** una configuración existente. Lo que hace:

- instala `python3-venv`, `python3-dev` y `bluez` con apt;
- crea el usuario de servicio `wtvb01`, en los grupos `dialout` (puertos
  serie) y `bluetooth` (política D-Bus de BlueZ);
- crea un entorno virtual en `/opt/wtvb01-logger/venv` con el core y el
  logger, incluido el extra `ble`;
- deja la configuración en `/etc/wtvb01-logger/config.toml`;
- instala y habilita `wtvb01-logger.service`;
- enlaza el comando en `/usr/local/bin/wtvb01-logger`;
- te añade al grupo `wtvb01` para que puedas mandar comandos de control sin
  `sudo` (**hay que cerrar sesión y volver a entrar** para que aplique).

---

## 4. Configuración

```bash
sudoedit /etc/wtvb01-logger/config.toml
```

### Descubrir qué hay conectado

```bash
wtvb01-logger ports
```

```bash
wtvb01-logger ble-scan
```

El primero lista los puertos serie —`/dev/ttyUSB0` para el puente CH340 que
lleva este sensor—; el segundo, las direcciones BLE, marcando con `*` las que
parecen sensores WitMotion. Copia lo que salga al archivo.

### Ejemplo mínimo

```toml
[logger]
output_dir = "/var/lib/wtvb01-logger"
rotate_minutes = 15
flush_seconds = 2
control_socket = "/run/wtvb01-logger/control.sock"

[defaults]
mode = "normal"
reconnect_seconds = 5
stall_seconds = 20
max_rate_hz = 0

[[sensors]]
name = "rotor-izq"
transport = "serial"
port = "/dev/ttyUSB0"

[[sensors]]
name = "rotor-der"
transport = "ble"
address = "AA:BB:CC:DD:EE:FF"
```

### Opciones por sensor

| Clave | Por defecto | Para qué |
|-------|-------------|----------|
| `name` | — | Nombre de carpeta y archivo. Sin espacios ni barras. |
| `transport` | — | `serial` o `ble` |
| `port` / `address` | — | Nodo de dispositivo, o dirección BLE |
| `baudrate` | 115200 | Solo para `serial` |
| `mode` | `normal` | `normal`, `high_speed` o `stream` |
| `poll_interval` | según modo | Segundos entre lecturas de registro |
| `reconnect_seconds` | 5 | Espera tras un fallo; crece con backoff hasta ×6 |
| `stall_seconds` | 20 | Rehace el enlace si no llega ninguna trama |
| `max_rate_hz` | 0 | Tope de filas por segundo. 0 = todo |
| `enabled` | `true` | `false` lo deja declarado pero sin arrancar |

### Comprobar antes de arrancar

```bash
wtvb01-logger -c /etc/wtvb01-logger/config.toml validate
```

La validación es estricta a propósito: **una clave mal escrita es un error, no
un silencio**. En un equipo sin pantalla, una opción ignorada no se nota hasta
que faltan los datos.

---

## 5. Arranque manual (comportamiento por defecto)

De fábrica **nada se ejecuta solo**. Al encender la Raspberry no arranca el
servicio, y aunque lo arranques no toca los sensores hasta que se lo pidas.
Son dos interruptores independientes:

| | Qué controla | Por defecto |
|---|---|---|
| `systemctl enable` | Que el servicio arranque al encender la Raspberry | **desactivado** |
| `connect_on_start` | Que el servicio tome los sensores al arrancar | **`false`** |

### Sesión típica

```bash
sudo systemctl start wtvb01-logger
```

El servicio queda **inactivo**: responde a los comandos pero no toca ninguna
radio ni ningún puerto serie. `status` muestra los sensores como
`SUELTO (sin conectar)`.

```bash
wtvb01-logger connect
```

Ahora sí toma los sensores y empieza a grabar. Comprueba con:

```bash
wtvb01-logger status
```

Para soltar los sensores **sin parar el servicio** — libera la radio, así
puedes usarlos en otra cosa:

```bash
wtvb01-logger disconnect
```

Y para volver a tomarlos, `connect` otra vez. Ambos aceptan
`--sensor <nombre>` para actuar sobre uno solo.

Para terminar del todo:

```bash
sudo systemctl stop wtvb01-logger
```

### `disconnect` no es `pause`

| Comando | Enlace con el sensor | Archivo | Para qué |
|---------|---------------------|---------|----------|
| `pause` | **se mantiene** | se mantiene abierto | Cortar la grabación un momento sin perder la conexión |
| `disconnect` | **se suelta** | se cierra | Liberar el sensor para usarlo en otra cosa |

Si lo que quieres es usar el sensor con otro equipo, tiene que ser
`disconnect`: un periférico BLE conectado deja de anunciarse, así que con
`pause` seguiría invisible para todo lo demás.

### Cuando quieras que sea automático

Los dos interruptores, por separado:

```bash
sudo systemctl enable wtvb01-logger
```

Eso hace que el servicio arranque al encender. Para que además **grabe solo**,
sin que nadie ejecute `connect` —lo que querrás para operación desatendida,
donde tras un corte de corriente no hay quien lo arranque— pon en
`/etc/wtvb01-logger/config.toml`:

```toml
[logger]
connect_on_start = true
```

Para volver atrás: `sudo systemctl disable wtvb01-logger`.

---

## 6. Uso diario

Todo por SSH, sin parar la captura ni soltar el enlace con los sensores:

| Comando | Efecto |
|---------|--------|
| `wtvb01-logger status` | Estado por sensor: enlace, modo, tasas, archivo, filas |
| `wtvb01-logger connect` | Toma los sensores y empieza a grabar |
| `wtvb01-logger disconnect` | **Suelta** los sensores y cierra los archivos |
| `wtvb01-logger roll` | Cierra los archivos actuales y empieza otros nuevos |
| `wtvb01-logger pause` | Deja de escribir, **sin** soltar el sensor |
| `wtvb01-logger resume` | Vuelve a escribir |
| `wtvb01-logger stop` | Para el servicio cerrando los archivos y liberando la radio |
| `wtvb01-logger ping` | Comprueba que el servicio responde |

`connect`, `disconnect`, `roll`, `pause` y `resume` aceptan
`--sensor <nombre>` para actuar sobre uno solo. Todos aceptan `--json` para
consumo por script.

Ejemplos:

```bash
ssh pi-zero wtvb01-logger status
```

```bash
ssh pi-zero wtvb01-logger roll --sensor rotor-izq
```

`systemctl reload wtvb01-logger` hace un `roll` completo por SIGHUP, por si el
socket no está a mano.

### Guardar los CSV en tu carpeta personal

Por defecto van a `/var/lib/wtvb01-logger`, que systemd crea y entrega al
servicio. Si prefieres tenerlos en tu home, pon la ruta en la configuración:

```toml
[logger]
output_dir = "/home/imago/wtvb01-logs"
```

y vuelve a ejecutar `sudo ./install.sh`. El instalador detecta que la ruta
está en `/home`, crea el directorio con los permisos correctos y genera un
*drop-in* de systemd que lo expone al servicio.

Hace falta porque la unidad usa `ProtectHome=yes`, que oculta `/home` entero
al servicio. El drop-in lo cambia por `ProtectHome=tmpfs` más un `BindPaths`
del directorio de logs: el servicio ve **solo** esa carpeta y nada más de tu
home. El directorio queda como `wtvb01:<tu usuario>` con setgid, así el
servicio escribe y tú lees.

A mano sería:

```bash
sudo mkdir -p /home/imago/wtvb01-logs && sudo chown wtvb01:imago /home/imago/wtvb01-logs && sudo chmod 2775 /home/imago/wtvb01-logs
```

```bash
sudo systemctl edit wtvb01-logger
```

y añadir `ProtectHome=tmpfs` y `BindPaths=/home/imago/wtvb01-logs` bajo
`[Service]`.

### Traerte los datos

```bash
rsync -avz pi-zero:/var/lib/wtvb01-logger/ ./datos/
```

---

## 7. Cada cuánto se crea un archivo nuevo

Cada `rotate_minutes` de grabación, por sensor. En el ejemplo son **15
minutos**; con `rotate_minutes = 1` sería cada minuto.

Además se abre un archivo nuevo, sin esperar al temporizador, en tres casos:

- al conectar un sensor (`connect`, o el arranque del servicio);
- cuando ejecutas `roll` (o `systemctl reload`, que hace lo mismo);
- tras un `disconnect` seguido de `connect`.

El temporizador cuenta desde que se abrió cada archivo, **no** está alineado
con el reloj: si empiezas a grabar a las 10:07 con rotación de 15 minutos, los
cortes caen a las 10:22, 10:37 y así, no a las 10:15 y 10:30.

Una reconexión automática tras una caída del enlace **no** abre archivo nuevo;
sigue en el mismo, con un hueco en las marcas de tiempo.

### Si no hay ningún sensor

No se crea ningún archivo. Ni siquiera vacío: el archivo se abre con la
primera muestra que llega, no al arrancar. Un sensor apagado o fuera de
alcance deja el servicio reintentando con espera creciente (de
`reconnect_seconds` hasta seis veces ese valor) y `status` lo muestra como
`SIN CONEXIÓN`.

En cuanto el sensor aparece, el siguiente reintento conecta y **empieza a
grabar solo**, sin que tengas que hacer nada. La espera es como mucho el techo
del backoff —30 s con los valores por defecto— más lo que tarde el escaneo
BLE, unos 12 s. Cuenta con hasta ~45 s desde que enciendes el sensor hasta la
primera fila.

`roll` no crea archivos para sensores que no están conectados.

## 8. Cuánto ocupa

Un sensor a 100 Hz escribe unas 100 filas por segundo, del orden de
**90 MB/hora**. Con `max_rate_hz` se diezma antes de escribir:

| `max_rate_hz` | Por sensor y hora |
|---------------|-------------------|
| 0 (todo) | ~90 MB |
| 50 | ~45 MB |
| 10 | ~9 MB |
| 1 | ~0.9 MB |

### Formato

Un directorio por sensor, y dentro `<sensor>-<AAAAMMDD-HHMMSS>.csv`:

```
/var/lib/wtvb01-logger/
├── rotor-izq/
│   ├── rotor-izq-20260904-100000.csv
│   └── rotor-izq-20260904-101500.csv
└── rotor-der/
    └── rotor-der-20260904-100000.csv
```

Columnas: `sensor`, `timestamp_iso`, `t_epoch`, y después un canal por
registro **en unidades físicas** — velocidad (mm/s), ángulo (°), temperatura
(°C), desplazamiento (µm), frecuencia (Hz), desplazamiento de alta velocidad
(µm), aceleración (g) y giro (°/s).

El juego de columnas es fijo, así que archivos de sensores o sesiones
distintas se concatenan sin alinear nada. Una celda vacía significa que ese
canal no llegó en esa muestra, lo cual depende del modo de captura.

---

## 9. Problemas conocidos

### El Bluetooth no ve nada

La imagen de Raspberry Pi OS trae el adaptador **bloqueado por rfkill**: BlueZ
corre pero la radio está apagada, y ningún escaneo devuelve sensores. La
unidad de systemd lo desbloquea al arrancar; a mano:

```bash
sudo rfkill unblock bluetooth && sudo hciconfig hci0 up
```

`hciconfig hci0` debe decir `UP RUNNING`.

### Un sensor no aparece en el escaneo

Un periférico BLE **conectado deja de anunciarse**. Si un proceso anterior
murió sin cerrar el enlace, BlueZ lo mantiene abierto y el sensor queda
invisible. Comprobar y liberar:

```bash
bluetoothctl devices Connected
```

```bash
bluetoothctl disconnect AA:BB:CC:DD:EE:FF
```

El servicio cierra los enlaces correctamente al pararse; esto solo pasa si se
mata a lo bruto (`kill -9`).

### `permission denied` en el socket de control

Falta estar en el grupo `wtvb01`, o no has vuelto a iniciar sesión desde que
el instalador te añadió:

```bash
id -nG | tr ' ' '\n' | grep wtvb01 || sudo usermod -aG wtvb01 "$USER"
```

### El servicio no arranca

```bash
sudo journalctl -u wtvb01-logger -n 50 --no-pager
```

Casi siempre es la configuración. Comprobarla con `validate`.

Dos causas vistas en la práctica:

**`is not UTF-8 text: byte 0xC3 ...`** — se coló un byte suelto al editar,
normalmente una tilde muerta del teclado que no llegó a componerse con la
letra siguiente. El mensaje dice la línea; borra ese carácter y guarda.

**`output_dir` en `/home` sin el drop-in** — el servicio no puede escribir
ahí porque `ProtectHome=yes` oculta `/home`. Ver
[§6](#guardar-los-csv-en-tu-carpeta-personal).

---

## 10. Limitaciones medidas

Medido en el hardware real, no estimado:

**Tasa de trama por BLE: ~50-90 Hz, variable.** Frente a ~100 Hz estables por
cable. Tres ejecuciones con el mismo sensor y sin escribir CSV dieron 102, 72
y 58 Hz, siempre con cero bytes descartados: no se pierden tramas al
decodificar, es el intervalo de conexión que negocia la radio lo que entrega
menos. **Si necesitas ancho de banda garantizado en la aceleración, usa
cable.**

**Varios sensores BLE a la vez: funciona, pero la conexión inicial es
frágil.** Dos sensores registrando en paralelo se sostuvieron sin perder una
fila (61 y 66 Hz, carga 0.68 en 4 núcleos, 190 MB de RAM libres). Pero en otra
ejecución uno de los dos no llegó a conectar nunca. La causa es que un
adaptador BlueZ admite **una sola sesión de descubrimiento**, y los dos
sensores escaneaban a la vez. Los escaneos ya están serializados con un
bloqueo compartido, **pero esa corrección no se ha vuelto a probar con dos
sensores** — quedó pendiente al necesitar los sensores para otra cosa. Es lo
primero que hay que verificar.

**El enlace BLE se cae de vez en cuando** y se rehace solo. Está gestionado:
el corte se detecta por `disconnected_callback`, y además hay un perro
guardián que rehace cualquier enlace que lleve `stall_seconds` sin entregar
una trama, incluso si dice estar conectado. Cada sensor reconecta por su
cuenta sin afectar a los demás.

---

## 11. Desinstalar

```bash
sudo systemctl disable --now wtvb01-logger
```

```bash
sudo rm -rf /etc/systemd/system/wtvb01-logger.service /opt/wtvb01-logger /usr/local/bin/wtvb01-logger && sudo systemctl daemon-reload
```

Los datos en `/var/lib/wtvb01-logger` y la configuración en
`/etc/wtvb01-logger` no se tocan; bórralos aparte si quieres.
