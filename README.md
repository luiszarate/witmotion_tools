# witmotion_tools

Herramientas para leer, visualizar y registrar datos de sensores WitMotion.

| Carpeta | Qué es |
|---------|--------|
| [`core/`](core/) | Librería del dispositivo: protocolo, mapa de registros, modos de captura y transportes (serie y BLE). Sin interfaz. |
| [`mac/`](mac/) | Monitor de escritorio para macOS: visualizador en tiempo real por puerto serie. |
| [`pi/`](pi/) | Servicio de logging desatendido para Raspberry Pi: varios sensores, CSV con rotación, control por SSH. |

Todo gira en torno al sensor de vibración **WitMotion WTVB01-BT50**.

## Empezar

Visualizar un sensor conectado por USB-C en el Mac:

```bash
cd mac && ./run.sh
```

Registrar varios sensores en una Raspberry Pi:

```bash
cd pi && sudo ./install.sh
```

El manual completo de despliegue y operación está en
[pi/DESPLIEGUE.md](pi/DESPLIEGUE.md): instalación, configuración, uso por SSH,
formato de los datos, problemas conocidos y limitaciones medidas.

## Protocolo

El [README del monitor](mac/README.md) documenta el protocolo verificado
contra hardware: tramas, mapa de registros, modos de captura y las trampas
que tiene este sensor (longitudes de trama distintas según el transporte,
canales que solo llegan por sondeo explícito).

## Pruebas

```bash
cd core && python3 -m unittest discover -s tests -t .
```

Cada carpeta tiene su propia suite; ninguna necesita el sensor conectado.
