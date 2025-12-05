#include <ArduinoJson.h> // Necessário para parsear o JSON da API
#include <DHT.h>         // Biblioteca DHT para umidade e temperatura
#include <HTTPClient.h>
#include <WiFi.h>

// Lista de redes WiFi
struct WifiConfig {
  const char *ssid;
  const char *password;
};

// Adicione quantas redes quiser
WifiConfig redes[] = {
    {"Renata fibra oi 2.4", "rafael26"},
    {"Bruno Matheus", "04021998"},
    {"jrpn", "joserob22"},
    {"iPhone", "Renata26"},
    {"Bruno", "Teresopolis040298@"},
};

// --- ENDPOINTS DA API ---
// IP atualizado conforme solicitado
const char *serverUrl_Sensores = "http://192.168.1.24:8080/api/sensores";
const char *serverUrl_Config = "http://192.168.1.24:8080/api/config_esp";

// --- VARIÁVEIS DE CONTROLE DA API ---
int config_circ = 0;  // Padrão: 0 (Circulação Desligada)
int config_nutri = 0; // Padrão: 0 (Nutrientes Desligados)

// ---- Relé ----
// Pinagem original
const int relayPins[4] = {2, 4, 5, 18};
const int RELAY_CIRC = relayPins[0];  // Relé 1 (Pino 2) para Circulação
const int RELAY_NUTRI = relayPins[1]; // Relé 2 (Pino 4) para Nutrientes

// ---- Sensor Ultrassônico ----
const int trigPin = 23;
const int echoPin = 22;

// ---- DHT11 ----
#define DHTPIN 21
#define DHTTYPE DHT11
DHT dht(DHTPIN, DHTTYPE);

// ---- TDS/Eletrocondutividade ----
const int TdsSensorPin = 33; // ADC seguro

// ---- Sensor de pH ----
const int pHSensorPin = 32; // ADC seguro para pH

const int totalRedes = sizeof(redes) / sizeof(redes[0]);

// Variáveis para controle de tempo (millis)
unsigned long lastConfigCheck = 0;
unsigned long lastSensorSend = 0;
const unsigned long INTERVAL_CONFIG = 500;   // 500ms
const unsigned long INTERVAL_SENSORS = 5000; // 5000ms (5s)

void conectarWiFi() {
  Serial.println("Iniciando tentativa de conexão WiFi...");

  for (int i = 0; i < totalRedes; i++) {
    Serial.printf("\nTentando conectar em: %s\n", redes[i].ssid);
    WiFi.begin(redes[i].ssid, redes[i].password);

    unsigned long inicioTentativa = millis();

    // Tentar conectar por 7 segundos
    while (WiFi.status() != WL_CONNECTED && millis() - inicioTentativa < 7000) {
      delay(500);
      Serial.print(".");
    }

    if (WiFi.status() == WL_CONNECTED) {
      Serial.printf("\nConectado com sucesso em %s!\n", redes[i].ssid);
      Serial.print("IP: ");
      Serial.println(WiFi.localIP());
      return;
    } else {
      Serial.printf("\nFalha ao conectar em %s\n", redes[i].ssid);
    }
  }

  Serial.println("\nNenhuma rede disponível no momento.");
}

void setup() {
  Serial.begin(115200);
  delay(1000);

  conectarWiFi();

  // Inicializa os pinos dos relés
  for (int i = 0; i < 4; i++) {
    pinMode(relayPins[i], OUTPUT);
    digitalWrite(relayPins[i], LOW); // Inicializa com LOW (Desligado)
  }

  digitalWrite(RELAY_CIRC, LOW);
  digitalWrite(RELAY_NUTRI, LOW);

  // Inicializa sensor ultrassônico
  pinMode(trigPin, OUTPUT);
  pinMode(echoPin, INPUT);

  // Inicia o sensor DHT11
  dht.begin();

  // Pino ADC para TDS
  pinMode(TdsSensorPin, INPUT);

  // Pino ADC para pH
  pinMode(pHSensorPin, INPUT);

  Serial.println("\nConectado ao Wi-Fi!");
  Serial.print("IP Local: ");
  Serial.println(WiFi.localIP());
  Serial.println("-------------------");
}

/**
 * Função para fazer a requisição GET na API e obter as configurações
 */
void get_config() {
  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    http.begin(serverUrl_Config);
    // Timeout curto para não bloquear muito tempo
    http.setTimeout(1000);

    int httpResponseCode = http.GET();

    if (httpResponseCode > 0) {
      String payload = http.getString();
      // Serial.println("Config: " + payload); // Debug reduzido

      DynamicJsonDocument doc(512);
      DeserializationError error = deserializeJson(doc, payload);

      if (!error) {
        config_circ = doc["config_circ"].as<int>();
        config_nutri = doc["config_nutri"].as<int>();

        // Controle dos Relés
        digitalWrite(RELAY_CIRC, config_circ == 1 ? HIGH : LOW);
        digitalWrite(RELAY_NUTRI, config_nutri == 1 ? HIGH : LOW);
      }
    }
    http.end();
  }
}

void read_and_send_sensors() {
  // Leitura sensor ultrassônico
  long duration, distance;
  digitalWrite(trigPin, LOW);
  delayMicroseconds(2);
  digitalWrite(trigPin, HIGH);
  delayMicroseconds(10);
  digitalWrite(trigPin, LOW);
  duration = pulseIn(echoPin, HIGH);
  distance = duration * 0.034 / 2;

  // Leitura sensor DHT11
  float humidity = dht.readHumidity();
  float temperature = dht.readTemperature();
  if (isnan(humidity) || isnan(temperature)) {
    temperature = 0.0;
    humidity = 0.0;
  }

  // Leitura sensor TDS
  int tdsValueRaw = analogRead(TdsSensorPin);
  float voltageTds = tdsValueRaw * (3.3 / 4095.0);
  float tds = (133.42 * voltageTds * voltageTds * voltageTds -
               255.86 * voltageTds * voltageTds + 857.39 * voltageTds) *
              0.5;

  // Leitura sensor pH
  int phValueRaw = analogRead(pHSensorPin);
  float voltagePh = phValueRaw * (3.3 / 4095.0);
  float ph = 7.0 + ((2.5 - voltagePh) / 0.18);

  // Cria o JSON
  String jsonData = "{";
  jsonData += "\"temperatura\": " + String(temperature, 2) + ",";
  jsonData += "\"umidade\": " + String(humidity, 2) + ",";
  jsonData += "\"distancia\": " + String(distance) + ",";
  jsonData += "\"ph\": " + String(ph, 2) + ",";
  jsonData += "\"eletrocondutividade\": " + String(tds, 2);
  jsonData += "}";

  Serial.println("Enviando: " + jsonData);

  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    http.begin(serverUrl_Sensores);
    http.addHeader("Content-Type", "application/json");
    http.setTimeout(3000); // Timeout razoável para POST

    int httpResponseCode = http.POST(jsonData);
    if (httpResponseCode > 0) {
      Serial.println("OK: " + String(httpResponseCode));
    } else {
      Serial.println("Erro POST: " + String(httpResponseCode));
    }
    http.end();
  } else {
    Serial.println("WiFi desconectado!");
    conectarWiFi(); // Tenta reconectar se caiu
  }
}

void loop() {
  unsigned long currentMillis = millis();

  // Tarefa 1: Buscar Configurações (500ms)
  if (currentMillis - lastConfigCheck >= INTERVAL_CONFIG) {
    lastConfigCheck = currentMillis;
    get_config();
  }

  // Tarefa 2: Ler e Enviar Sensores (5000ms)
  if (currentMillis - lastSensorSend >= INTERVAL_SENSORS) {
    lastSensorSend = currentMillis;
    read_and_send_sensors();
  }
}
