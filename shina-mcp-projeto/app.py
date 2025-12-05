import os
import time
import json
import redis
import serial
import serial.tools.list_ports
from dotenv import load_dotenv

# =========================
# Configuração do Redis
# =========================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
dotenv_path = os.path.join(BASE_DIR, ".env")
print("Usando arquivo .env em:", dotenv_path)
load_dotenv(dotenv_path=dotenv_path)

REDIS_URL = os.environ.get("REDIS_URL")
print("DEBUG REDIS_URL lido:", REDIS_URL)

if not REDIS_URL:
    print("ERRO: variável de ambiente REDIS_URL não está definida.")
    r = None
else:
    try:
        r = redis.from_url(REDIS_URL)
        print(f"Conectando ao Redis em: {REDIS_URL}")
        r.ping()
        print("Conexão com Redis OK!")
    except Exception as e:
        print(f"ERRO ao conectar no Redis: {e}")
        r = None

# =========================
# Configuração da Serial
# =========================

SERIAL_PORT = "COM4"  # atualize para sua porta, se necessário
BAUD_RATE = 115200

def get_serial_connection():
    """
    Tenta encontrar e conectar em um Arduino/ESP32 pela porta serial.
    """
    if SERIAL_PORT:
        try:
            ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
            print(f"Conectado ao Arduino/ESP32 em {SERIAL_PORT}")
            return ser
        except Exception as e:
            print(f"Não foi possível abrir {SERIAL_PORT}: {e}")

    ports = list(serial.tools.list_ports.comports())
    for p in ports:
        if "Arduino" in p.description or "USB" in p.description:
            try:
                ser = serial.Serial(p.device, BAUD_RATE, timeout=1)
                print(f"Conectado automaticamente ao Arduino/ESP32 em {p.device}")
                return ser
            except Exception as e:
                print(f"Falha ao conectar em {p.device}: {e}")
    print("Nenhum Arduino/ESP32 encontrado.")
    return None

def normalize_data(raw):
    """
    Converte campos em português do ESP32 para os nomes usados internamente.
    Aceita tanto chaves em português quanto em inglês.
    """
    if not isinstance(raw, dict):
        return None

    data = {}

    # pH
    # Calibração linear baseada em leituras: 2.5(real)->2.5(lido) e 8.0(real)->4.3(lido)
    # m = (8.0 - 2.5) / (4.3 - 2.5) = 3.0556
    # y = m * x + b  =>  2.5 = 3.0556 * 2.5 + b  =>  b = -5.139
    if "ph" in raw:
        raw_ph = float(raw["ph"])
        data["ph"] = (raw_ph * 3.0556) - 5.139
    elif "pH" in raw:
        raw_ph = float(raw["pH"])
        data["ph"] = (raw_ph * 3.0556) - 5.139

    # temperatura
    if "temperatura" in raw:
        data["temperature"] = float(raw["temperatura"])
    elif "temperature" in raw:
        data["temperature"] = float(raw["temperature"])

    # umidade
    if "umidade" in raw:
        data["humidity"] = float(raw["umidade"])
    elif "humidity" in raw:
        data["humidity"] = float(raw["humidity"])

    # salinidade
    if "salinidade" in raw:
        data["salinity"] = float(raw["salinidade"])
    elif "salinity" in raw:
        data["salinity"] = float(raw["salinity"])
    else:
        if "eletrocondutividade" in raw:
            data["salinity"] = float(raw["eletrocondutividade"])

    # condutividade
    if "eletrocondutividade" in raw:
        data["conductivity"] = float(raw["eletrocondutividade"])
    elif "conductivity" in raw:
        data["conductivity"] = float(raw["conductivity"])

    # distancia
    if "distancia" in raw:
        data["distance"] = float(raw["distancia"])

    if "timestamp" in raw:
        data["timestamp"] = raw["timestamp"]

    return data

def read_from_serial(ser):
    """
    Lê uma linha JSON da serial, normaliza os campos e adiciona timestamp.
    Espera algo como: {"temperatura": 24.5, "umidade": 60.2, "ph": 6.3, ...}
    """
    if ser and ser.in_waiting > 0:
        try:
            line = ser.readline().decode("utf-8").strip()
            if line:
                print(f"[SERIAL] Recebido bruto: {line}")
                raw = json.loads(line)

                data = normalize_data(raw)
                if not data:
                    print("[SERIAL] Dados inválidos após normalização.")
                    return None

                if "timestamp" not in data:
                    data["timestamp"] = int(time.time() * 1000)

                print(f"[SERIAL] Normalizado: {data}")
                return data
        except Exception as e:
            print(f"Erro ao ler da serial: {e}")
    return None

def process_data(data):
    """
    Processa os dados reais e grava no Redis:
    - Stream: sensors:stream
    - Hash da última leitura: sensors:latest
    - Ranking de temperatura: sensors:ranking:temp
    - Pub/Sub de alertas críticos: canal 'alerts'
    """
    if not r:
        print("Redis não conectado. Dados não serão gravados.")
        return

    try:
        r.xadd("sensors:stream", data)
        r.hset("sensors:latest", mapping=data)
        member_id = f"reading:{data['timestamp']}"
        r.zadd("sensors:ranking:temp", {member_id: data["temperature"]})

        if data.get("ph", 0) < 5.8 or data.get("ph", 14) > 6.8:
            alert = {
                "type": "CRITICAL",
                "message": f"pH fora da faixa ideal: {data['ph']}",
                "timestamp": data["timestamp"],
            }
            r.publish("alerts", json.dumps(alert))
            print(f"[ALERTA] {alert['message']}")

        print(f"[OK] Dados processados e enviados ao Redis: {data}")

    except Exception as e:
        print(f"Erro ao processar dados no Redis: {e}")

def main():
    print("Iniciando backend IoT da hidroponia...")

    if not r:
        print("ERRO: sem conexão Redis. Verifique REDIS_URL e tente novamente.")
        return

    ser = get_serial_connection()

    if not ser:
        print("Nenhum Arduino/ESP32 conectado via serial. Abortando.")
        return

    while True:
        sensor_data = read_from_serial(ser)

        if not sensor_data:
            time.sleep(0.1)
            continue

        process_data(sensor_data)

if __name__ == "__main__":
    main()
