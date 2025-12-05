import os
import redis
import time
import asyncio
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

# =========================
# Conexão com Redis
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
dotenv_path = os.path.join(BASE_DIR, ".env")
load_dotenv(dotenv_path=dotenv_path)

REDIS_URL = os.environ.get("REDIS_URL")
if not REDIS_URL:
    # Fallback para localhost se não definido (útil para dev local fora do docker)
    REDIS_URL = "redis://localhost:6379/0"
    print(f"AVISO: REDIS_URL não definido. Usando padrão: {REDIS_URL}")

print(f"DEBUG: Usando REDIS_URL: {REDIS_URL}")

try:
    r = redis.from_url(REDIS_URL)
    r.ping()
    print("Conexão com Redis OK!")
except Exception as e:
    print(f"ERRO ao conectar no Redis: {e}")
    r = None

# =========================
# App FastAPI
# =========================
app = FastAPI(title="Shina Hydroponic API")

# CORS simples (permite dashboard local e ESP32 comunicarem com API)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class PumpConfig(BaseModel):
    config_circ: bool
    config_nutri: bool
    config_auto: bool = False # Automação Nutrientes
    config_circ_auto: bool = False # Automação Circulação

class CalibrationPoint(BaseModel):
    voltage: float
    ph: float

class ChatRequest(BaseModel):
    message: str

def decode_dict(d):
    """Converte dict de bytes -> str."""
    return {
        (k.decode() if isinstance(k, bytes) else k):
        (v.decode() if isinstance(v, bytes) else v)
        for k, v in d.items()
    }

# =========================
# Lógica de Calibração de pH
# =========================
def estimate_voltage_from_ph(arduino_ph: float) -> float:
    """
    Reverte a fórmula do Arduino para estimar a voltagem lida.
    Fórmula Arduino: ph = 7.0 + ((2.5 - voltage) / 0.18)
    Inversa: voltage = 2.5 - ((ph - 7.0) * 0.18)
    """
    return 2.5 - ((arduino_ph - 7.0) * 0.18)

def calculate_calibrated_ph(arduino_ph: float) -> float:
    """
    Calcula o pH calibrado baseado nos pontos salvos no Redis.
    Usa regressão linear simples (y = mx + b) se houver 2 pontos.
    Se houver 1 ponto, ajusta o offset.
    """
    if not r: return arduino_ph
    
    # Buscar pontos de calibração
    points_data = r.hgetall("calibration:ph:points")
    if not points_data:
        return arduino_ph # Sem calibração
        
    points = []
    for k, v in points_data.items():
        try:
            points.append({"voltage": float(k), "ph": float(v)})
        except:
            pass
            
    if not points:
        return arduino_ph

    # Estimar voltagem atual
    current_voltage = estimate_voltage_from_ph(arduino_ph)

    # Lógica de Calibração
    if len(points) == 1:
        # Calibração de 1 ponto (Offset)
        # Assumindo que o slope ideal é -0.18 (do Arduino) -> ph = m*v + b
        # m = -1/0.18 = -5.55 approx (mas a fórmula do arduino é (2.5-v)/0.18)
        # Vamos usar a diferença simples
        # Ponto conhecido: (v_ref, ph_ref)
        # ph_ref = 7.0 + ((2.5 - v_ref) / 0.18) -> Isso é o teórico.
        # Vamos calcular o offset real.
        # ph_real = ph_arduino + offset
        # offset = ph_ref - ph_arduino_no_ponto
        # Mas aqui temos voltagem.
        # Vamos simplificar: Calcular novo intercept 'b' mantendo slope padrão.
        # Fórmula padrão: ph = 7.0 + (2.5 - v) / 0.18
        # ph = 7.0 + 13.88 - 5.55v = 20.88 - 5.55v
        # Slope padrão ~ -5.55
        
        p = points[0]
        # Novo b = ph_ref - m * v_ref
        m = -5.5555 # Slope padrão
        b = p["ph"] - (m * p["voltage"])
        
        calibrated_ph = (m * current_voltage) + b
        return round(calibrated_ph, 2)

    elif len(points) >= 2:
        # Calibração de 2 pontos (Linear)
        # Ordenar por voltagem
        points.sort(key=lambda x: x["voltage"])
        p1 = points[0]
        p2 = points[-1]
        
        # Calcular slope (m) e intercept (b)
        if (p2["voltage"] - p1["voltage"]) == 0:
            return arduino_ph # Evitar divisão por zero
            
        m = (p2["ph"] - p1["ph"]) / (p2["voltage"] - p1["voltage"])
        b = p1["ph"] - (m * p1["voltage"])
        
        calibrated_ph = (m * current_voltage) + b
        return round(calibrated_ph, 2)
        
    return arduino_ph

# =========================
# Lógica de Automação
# =========================
async def turn_off_nutri_after_delay(delay: float):
    """Desliga a bomba de nutrientes após X segundos."""
    await asyncio.sleep(delay)
    if r:
        r.hset("config:pumps", "config_nutri", 0)
        print(f"[AUTOMAÇÃO] Bomba de nutrientes DESLIGADA após {delay}s.")

async def turn_off_circ_after_delay(delay: float):
    """Desliga a bomba de circulação após X segundos."""
    await asyncio.sleep(delay)
    if r:
        r.hset("config:pumps", "config_circ", 0)
        print(f"[AUTOMAÇÃO] Bomba de circulação DESLIGADA após {delay}s.")

def check_circulation_automation(background_tasks: BackgroundTasks):
    """
    Automação da Bomba de Circulação.
    Regra: Ligar 1 min a cada 15 min.
    """
    if not r: return

    config = r.hgetall("config:pumps")
    config = decode_dict(config)

    # 1. Verificar se Automação de Circulação está ligada
    if int(config.get("config_circ_auto", 0)) != 1:
        return

    # 2. Verificar Ciclo (15 min = 900s)
    last_cycle = r.get("circ:last_cycle")
    now = time.time()
    
    # Se nunca rodou ou já passou 15 min
    if not last_cycle or (now - float(last_cycle) > 120):
        print(f"[AUTOMAÇÃO] Iniciando ciclo de circulação...")
        
        # Ligar bomba
        r.hset("config:pumps", "config_circ", 1)
        
        # Atualizar timestamp do último ciclo
        r.set("circ:last_cycle", now)
        
        # Agendar desligamento (1 min = 60s)
        background_tasks.add_task(turn_off_circ_after_delay, 60)
        print(f"[AUTOMAÇÃO] Bomba de circulação LIGADA por 60s.")

def check_and_dose_nutrients(conductivity: float, background_tasks: BackgroundTasks):
    """
    Verifica se precisa dosar nutrientes.
    Regra: Se cond < 1.7 E circulação ligada E cooldown > 15min E AUTOMACAO LIGADA.
    """
    if not r: return

    # 0. Verificar se Automação de Nutrientes está ligada
    config = r.hgetall("config:pumps")
    config = decode_dict(config)
    
    if int(config.get("config_auto", 0)) != 1:
        # Automação desligada. Não faz nada, a menos que usuário ligue manual.
        return

    # 1. Verificar Condutividade (Prioridade)
    if conductivity < 310.0:
        print(f"[AUTOMAÇÃO] Condutividade baixa ({conductivity}). Verificando condições...")

        # 2. Verificar se circulação está ligada
        if int(config.get("config_circ", 0)) != 1:
            print(f"[AUTOMAÇÃO] Circulação estava desligada. Forçando LIGAR para misturar nutrientes.")
            r.hset("config:pumps", "config_circ", 1)
            # Retorna para dar tempo da água circular antes de dosar (próxima leitura)
            return 

        # 3. Verificar Cooldown (Reduzido para 60s para testes)
        last_dose = r.get("nutri:last_dose")
        now = time.time()
        if last_dose and (now - float(last_dose) < 60):
            remaining = 60 - (now - float(last_dose))
            print(f"[AUTOMAÇÃO] Skipped: Cooldown ativo ({int(remaining)}s restantes). Cond: {conductivity}")
            return # Cooldown ativo

        # 4. Executar Dosagem
        print(f"[AUTOMAÇÃO] Iniciando dosagem de nutrientes...")
        
        # Configurar dosagem
        DOSE_ML = 10
        # Tempo = DOSE / 2.0 (assumindo vazão de 2.0 ml/s)
        dose_time = DOSE_ML / 2.0
        
        # Ligar bomba
        r.hset("config:pumps", "config_nutri", 1)
        
        # Atualizar timestamp da última dosagem
        r.set("nutri:last_dose", now)
        
        # Agendar desligamento
        background_tasks.add_task(turn_off_nutri_after_delay, dose_time)
        print(f"[AUTOMAÇÃO] Bomba de nutrientes LIGADA por {dose_time}s.")
    else:
        # Condutividade OK
        pass

# =========================
# Endpoints
# =========================

@app.get("/api/latest")
def get_latest():
    """
    Retorna a última leitura dos sensores.
    Lê o hash sensors:latest.
    Se os dados forem muito antigos (> 30s), retorna zeros (Sistema Offline).
    """
    if not r: raise HTTPException(status_code=503, detail="Redis unavailable")
    data = r.hgetall("sensors:latest")
    decoded = decode_dict(data)
    
    # Verificar frescor dos dados
    # Timestamp salvo em ms
    last_ts = float(decoded.get("timestamp", 0)) / 1000.0 
    now = time.time()
    
    # Se dados mais velhos que 30 segundos, retornar zeros
    if (now - last_ts) > 30:
        return {
            "ph": 0,
            "temperature": 0,
            "humidity": 0,
            "conductivity": 0,
            "distance": 0,
            "water_level": 0,
            "timestamp": decoded.get("timestamp", 0),
            "status": "offline"
        }
        
    return decoded

@app.get("/api/stream")
def get_stream(limit: int = 50):
    """
    Retorna as últimas N leituras do stream.
    Usa XREVRANGE para pegar do mais recente para o mais antigo.
    """
    if not r: raise HTTPException(status_code=503, detail="Redis unavailable")
    entries = r.xrevrange("sensors:stream", count=limit)
    result = []
    for entry_id, fields in entries:
        decoded_fields = decode_dict(fields)
        # Adiciona timestamp aproximado baseado no ID do stream se não houver no corpo
        # Redis Stream ID é timestamp-sequence
        ts_ms = int(entry_id.decode().split("-")[0])
        decoded_fields["timestamp_ms"] = ts_ms
        
        result.append({
            "id": entry_id.decode() if isinstance(entry_id, bytes) else entry_id,
            **decoded_fields,
        })
    return result

@app.get("/api/ranking/temperature")
def get_temperature_ranking(limit: int = 10):
    """
    Retorna ranking de temperaturas (Sorted Set).
    """
    if not r: raise HTTPException(status_code=503, detail="Redis unavailable")
    items = r.zrevrange("sensors:ranking:temp", 0, limit - 1, withscores=True)
    result = []
    for member, score in items:
        result.append({
            "id": member.decode() if isinstance(member, bytes) else member,
            "temperature": score,
        })
    return result

@app.post("/api/sensores")
async def ingest_sensores(request: Request, background_tasks: BackgroundTasks):
    """
    Recebe JSON de dados reais do ESP32 via Wi-Fi (HTTP POST).
    Normaliza chaves e Atualiza hash, stream e ranking no Redis.
    """
    if not r: raise HTTPException(status_code=503, detail="Redis unavailable")
    raw_body = await request.json()
    print("[SENSORES] Dados brutos recebidos:", raw_body)

    # 1. Normalização (Português -> Inglês)
    body = {}
    # Mapeamento
    # O Arduino envia TDS em ppm. Convertemos para mS/cm (aproximadamente ppm / 500)
    raw_cond = float(raw_body.get("eletrocondutividade", raw_body.get("conductivity", 0)))
    body["conductivity"] = round(raw_cond*1000 / 500.0, 2)
    
    body["distance"] = raw_body.get("distancia", raw_body.get("distance", 0))
    
    # Converter Distância (cm) para Volume (Litros)
    # Tanque: 22cm altura útil (vazio) -> 2cm (cheio, 10L)
    # Fórmula: Litros = 11 - (distancia / 2)
    dist_val = float(body["distance"])
    water_level = 11.0 - (dist_val / 2.0)
    # Clamp 0-10
    water_level = max(0.0, min(10.0, water_level))
    body["water_level"] = round(water_level, 2)
    
    print(f"[DEBUG] Raw Distance: {dist_val} cm -> Water Level: {water_level} L")
    print(f"[DEBUG] Raw Cond: {raw_cond} -> Converted: {body['conductivity']} uS/cm")
    body["temperature"] = raw_body.get("temperatura", raw_body.get("temperature", 0))
    body["humidity"] = raw_body.get("umidade", raw_body.get("humidity", 0))
    
    # pH (Calibração no Backend - Ajuste Fino 3)
    # 1. Reverter a fórmula do Arduino para achar a voltagem original
    # Arduino: ph = 7.0 + ((2.5 - v) / 0.18)
    raw_ph_arduino = float(raw_body.get("ph", 0))
    estimated_voltage = 2.5 - ((raw_ph_arduino - 7.0) * 0.18)
    
    # 2. Aplicar Nova Fórmula Calibrada (Baseada no feedback: 5.0->8.0 e 3.3->2.5)
    # Pontos recalculados: 
    # Leitura 5.0 (com fórmula anterior) -> V ~1.55V -> Target 8.0
    # Leitura 3.3 (com fórmula anterior) -> V ~1.69V -> Target 2.5
    # Nova Fórmula: pH = -39.01 * V + 68.48
    calibrated_ph = (-39.01 * estimated_voltage) + 68.48
    
    # 3. Média Móvel (pH)
    # Adiciona à lista e mantém apenas os últimos 10 (Solicitado pelo usuário)
    r.lpush("window:ph", calibrated_ph)
    r.ltrim("window:ph", 0, 9)
    # Calcula a média
    ph_window = [float(x) for x in r.lrange("window:ph", 0, -1)]
    avg_ph = sum(ph_window) / len(ph_window) if ph_window else calibrated_ph
    
    print(f"[DEBUG] pH Raw: {raw_ph_arduino} | Calib: {calibrated_ph:.2f} | Window Size: {len(ph_window)} | Avg: {avg_ph:.2f}")

    body["ph"] = round(avg_ph, 2)
    body["raw_ph"] = raw_ph_arduino 
    
    # Eletrocondutividade (Média Móvel)
    # O valor já foi convertido nas linhas acima (body['conductivity'])
    raw_ec = float(body.get("conductivity", 0))
    r.lpush("window:ec", raw_ec)
    r.ltrim("window:ec", 0, 9)
    ec_window = [float(x) for x in r.lrange("window:ec", 0, -1)]
    avg_ec = sum(ec_window) / len(ec_window) if ec_window else raw_ec
    
    print(f"[DEBUG] EC Raw: {raw_ec} | Window Size: {len(ec_window)} | Avg: {avg_ec:.2f}")

    body["conductivity"] = round(avg_ec, 2)
    body["eletrocondutividade"] = round(avg_ec, 2) # Manter compatibilidade
    
    # Adiciona timestamp atual
    body["timestamp"] = int(time.time() * 1000)

    # 2. Persistência
    # Atualiza último valor dos sensores
    r.hset("sensors:latest", mapping=body)
    # Adiciona ao stream para histórico
    r.xadd("sensors:stream", body)
    
    # Adiciona ao ranking de temperatura
    try:
        temp_val = float(body["temperature"])
        timestamp = body["timestamp"]
        r.zadd("sensors:ranking:temp", {f"reading:{timestamp}": temp_val})
    except Exception:
        pass

    # 3. Automação de Nutrientes
    try:
        cond_val = float(body["conductivity"])
        check_and_dose_nutrients(cond_val, background_tasks)
    except Exception as e:
        print(f"Erro na automação nutri: {e}")

    # 4. Automação de Circulação
    try:
        check_circulation_automation(background_tasks)
    except Exception as e:
        print(f"Erro na automação circ: {e}")

    return {"status": "ok", "normalized_data": body}

@app.get("/api/config_esp")
def get_config_esp():
    """
    Retorna a configuração atual das bombas para o ESP32.
    Lê do Redis (hash config:pumps).
    """
    if not r: raise HTTPException(status_code=503, detail="Redis unavailable")
    
    # Padrão se não existir
    default_config = {"config_circ": "0", "config_nutri": "0", "config_auto": "0", "config_circ_auto": "0"}
    
    data = r.hgetall("config:pumps")
    if not data:
        data = default_config
    else:
        data = decode_dict(data)
    
    # Converte para int/bool conforme esperado pelo ESP32 (0 ou 1)
    return {
        "config_circ": int(data.get("config_circ", 0)),
        "config_nutri": int(data.get("config_nutri", 0)),
        "config_auto": int(data.get("config_auto", 0)),
        "config_circ_auto": int(data.get("config_circ_auto", 0))
    }

@app.post("/api/config_esp")
def set_config_esp(config: PumpConfig):
    """
    Atualiza a configuração das bombas (via Frontend).
    Salva no Redis.
    """
    if not r: raise HTTPException(status_code=503, detail="Redis unavailable")
    
    mapping = {
        "config_circ": 1 if config.config_circ else 0,
        "config_nutri": 1 if config.config_nutri else 0,
        "config_auto": 1 if config.config_auto else 0,
        "config_circ_auto": 1 if config.config_circ_auto else 0
    }
    
    r.hset("config:pumps", mapping=mapping)
    print(f"[CONFIG] Bombas atualizadas: {mapping}")
    return {"status": "ok", "config": mapping}

# =========================
# Endpoints de Calibração
# =========================
@app.post("/api/calibration/ph/reset")
def reset_ph_calibration():
    """Reseta a calibração de pH."""
    if not r: raise HTTPException(status_code=503, detail="Redis unavailable")
    r.delete("calibration:ph:points")
    return {"status": "ok", "message": "Calibração de pH resetada."}

@app.post("/api/calibration/ph/point")
def add_ph_calibration_point(point: CalibrationPoint):
    """
    Adiciona um ponto de calibração.
    O frontend envia o valor de pH conhecido (ex: 7.0) e o sistema
    pega o último valor lido do Arduino para calcular a voltagem correspondente.
    OU o frontend envia a voltagem se já tiver calculado.
    
    Neste caso, vamos simplificar: O frontend manda o pH conhecido.
    Nós pegamos o último pH lido do sensor (sensors:latest -> ph)
    e revertemos para voltagem.
    """
    if not r: raise HTTPException(status_code=503, detail="Redis unavailable")
    
    # Pegar último valor lido (que é o valor 'errado' do Arduino)
    # IMPORTANTE: Precisamos do valor SEM calibração. 
    # Mas o sensors:latest já vai estar calibrado se tiver pontos.
    # Dilema.
    # Solução: O frontend deve mandar o valor 'raw' que ele está vendo agora?
    # Não, o frontend vê o valor calibrado.
    # Vamos assumir que durante a calibração, o usuário reseta primeiro.
    
    # Melhor: O frontend manda o pH do buffer (ex: 7.0).
    # Nós pegamos o valor atual do sensor. Se já estiver calibrado, precisamos 'descalibrar' para achar a voltagem?
    # Sim. Mas é complexo.
    # Abordagem mais robusta: Salvar também o 'raw_ph' no Redis em sensors:latest?
    # Vamos alterar o ingest_sensores para salvar 'raw_ph' também.
    
    data = r.hgetall("sensors:latest")
    if not data:
        raise HTTPException(status_code=400, detail="Sem dados do sensor para calibrar.")
        
    data = decode_dict(data)
    # Se tiver raw_ph, usa. Se não, usa ph (assumindo que é raw se não tiver calibração)
    raw_ph_val = float(data.get("raw_ph", data.get("ph", 0)))
    
    # Estimar voltagem
    voltage = estimate_voltage_from_ph(raw_ph_val)
    
    # Salvar ponto: chave=voltagem, valor=ph_real
    r.hset("calibration:ph:points", str(voltage), str(point.ph))
    
    return {"status": "ok", "message": f"Ponto salvo: {point.ph}pH @ {voltage:.3f}V (Lido: {raw_ph_val})"}

@app.get("/api/calibration/status")
def get_calibration_status():
    """Retorna status da calibração."""
    if not r: raise HTTPException(status_code=503, detail="Redis unavailable")
    
    points_data = r.hgetall("calibration:ph:points")
    points = []
    if points_data:
        for k, v in points_data.items():
            points.append({"voltage": float(k), "ph": float(v)})
            
    return {
        "is_calibrated": len(points) >= 2,
        "points_count": len(points),
        "points": points
    }

# =========================
# Integração com Gemini (LLM)
# =========================
import google.generativeai as genai

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.0-flash')
    print("DEBUG: Gemini API configurada.")
else:
    model = None
    print("AVISO: GEMINI_API_KEY não encontrada. O assistente funcionará em modo limitado.")

@app.post("/api/chat")
def chat_assistant(req: ChatRequest):
    """
    Assistente inteligente usando Google Gemini.
    Analisa dados atuais e histórico recente para responder perguntas.
    """
    if not r: return {"response": "Erro: Banco de dados indisponível."}
    
    # 1. Coletar Contexto (Dados Atuais)
    data = r.hgetall("sensors:latest")
    data = decode_dict(data)
    
    if not data:
        return {"response": "Ainda não tenho dados dos sensores. O sistema está conectado?"}

    # 2. Coletar Histórico Recente (Últimas 10 leituras para tendência)
    history_entries = r.xrevrange("sensors:stream", count=10)
    history_context = []
    for _, fields in history_entries:
        f = decode_dict(fields)
        history_context.append(
            f"T={f.get('temperature')}C, pH={f.get('ph')}, EC={f.get('conductivity')}, Nível={f.get('water_level')}L"
        )
    history_str = "\n".join(history_context)

    # 3. Montar Prompt do Sistema
    system_prompt = f"""
    Você é o Assistente SHINA, um especialista em hidroponia.
    Sua missão é ajudar o usuário a manter suas plantas saudáveis.
    
    DADOS EM TEMPO REAL:
    - pH: {data.get('ph')} (Ideal: 5.5 a 6.5)
    - Temperatura: {data.get('temperature')}°C (Ideal: 18 a 28°C)
    - Condutividade (EC): {data.get('conductivity')} uS/cm (Ideal: 800 a 1500 dependendo da planta)
    - Nível do Reservatório: {data.get('water_level')} Litros (Máx: 10L)
    - Umidade do Ar: {data.get('humidity')}%
    
    HISTÓRICO RECENTE (Do mais novo para o mais antigo):
    {history_str}
    
    PERGUNTA DO USUÁRIO: "{req.message}"
    
    INSTRUÇÕES:
    1. Responda de forma concisa e amigável.
    2. Use os dados fornecidos para embasar sua resposta.
    3. Se houver algo crítico (pH muito alto/baixo, falta de água), alerte o usuário imediatamente.
    4. Se a pergunta não for sobre hidroponia ou o sistema, responda educadamente que seu foco é as plantas.
    5. Responda sempre em Português do Brasil.
    """

    # 4. Chamar LLM
    if model:
        try:
            response = model.generate_content(system_prompt)
            return {"response": response.text}
        except Exception as e:
            print(f"Erro na API Gemini: {e}")
            return {"response": "Desculpe, estou com dificuldade de conectar ao meu cérebro agora. Mas os dados estão na tela!"}
    else:
        # Fallback se não tiver chave
        return {"response": "Estou sem minha chave de API (Gemini). Verifique o arquivo .env!"}


