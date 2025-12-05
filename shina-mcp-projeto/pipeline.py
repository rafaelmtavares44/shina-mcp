import os
import redis
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, window, avg, max, min, stddev
from pyspark.sql.types import StructType, StructField, StringType, FloatType, TimestampType
from dotenv import load_dotenv

# =========================
# Configuração
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
dotenv_path = os.path.join(BASE_DIR, ".env")
load_dotenv(dotenv_path=dotenv_path)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_DB   = int(os.environ.get("REDIS_DB", "0"))

STREAM_KEY = os.environ.get("REDIS_STREAM_KEY", "sensors:stream")

# =========================
# Funções Redis (leitura)
# =========================
def get_redis_connection():
    return redis.from_url(REDIS_URL)

def fetch_data_from_redis(r, stream_key=STREAM_KEY, count=1000):
    """
    Lê dados do Redis Stream e retorna uma lista de dicionários.
    """
    entries = r.xrevrange(stream_key, count=count)
    data = []
    for entry_id, fields in entries:
        record = {k.decode(): v.decode() for k, v in fields.items()}
        record["stream_id"] = entry_id.decode()

        # timestamp em ms -> s
        if "timestamp" in record:
            try:
                ts_ms = float(record["timestamp"])
                record["timestamp_sec"] = ts_ms / 1000.0
            except Exception:
                record["timestamp_sec"] = None

        data.append(record)
    return data

# =========================
# Spark Session (com spark-redis)
# =========================
def create_spark_session():
    """
    Cria SparkSession já preparado para falar com Redis via spark-redis.
    - Requer o pacote com.redislabs:spark-redis no spark-submit ou em spark.jars.packages.
    """
    return (
        SparkSession.builder
        .appName("ShinaDataPipeline")
        .master("local[*]")
        # Config de conexão com Redis para spark-redis
        .config("spark.redis.host", REDIS_HOST)
        .config("spark.redis.port", REDIS_PORT)
        .config("spark.redis.db", REDIS_DB)
        # Se você disparar via spark-submit, pode tirar a linha abaixo e passar o --packages lá
        .config("spark.jars.packages", "com.redislabs:spark-redis_2.12:2.4.0")
        .getOrCreate()
    )

# =========================
# Pipeline principal
# =========================
def process_data():
    print("--- Iniciando Pipeline de Dados SHINA ---")

    # 1. Conectar ao Redis e buscar dados brutos
    try:
        r = get_redis_connection()
        r.ping()
        print(f"Conectado ao Redis em {REDIS_URL}.")
    except Exception as e:
        print(f"Erro ao conectar ao Redis: {e}")
        return

    raw_data = fetch_data_from_redis(r)
    print(f"Registros lidos do Redis: {len(raw_data)}")

    if not raw_data:
        print("Nenhum dado para processar.")
        return

    # 2. Inicializar Spark
    spark = create_spark_session()
    spark.sparkContext.setLogLevel("WARN")

    # 3. Criar DataFrame com schema esperado
    schema = StructType([
        StructField("temperature",   StringType(), True),
        StructField("humidity",      StringType(), True),
        StructField("ph",            StringType(), True),
        StructField("conductivity",  StringType(), True),
        StructField("distance",      StringType(), True),
        StructField("timestamp_sec", FloatType(),  True),
        StructField("stream_id",     StringType(), True),
    ])

    df = spark.createDataFrame(raw_data, schema=schema)

    # 4. Limpeza e Conversão de Tipos
    df_clean = (
        df
        .withColumn("temperature",  col("temperature").cast(FloatType()))
        .withColumn("humidity",     col("humidity").cast(FloatType()))
        .withColumn("ph",           col("ph").cast(FloatType()))
        .withColumn("conductivity", col("conductivity").cast(FloatType()))
        .withColumn("distance",     col("distance").cast(FloatType()))
        # converte epoch seconds -> TimestampType
        .withColumn("timestamp",    (col("timestamp_sec") * 1_000).cast("timestamp"))
        .filter(col("timestamp").isNotNull())
    )

    print("\n--- Amostra de Dados Limpos ---")
    df_clean.show(5, truncate=False)

    # 5. Detecção de Outliers (regras simples)
    df_filtered = df_clean.filter(
        (col("ph") >= 0) & (col("ph") <= 14) &
        (col("temperature") > -10) & (col("temperature") < 60)
    )

    outliers_count = df_clean.count() - df_filtered.count()
    print(f"Outliers removidos: {outliers_count}")

    # 6. Agregação em janelas de 1 hora
    df_agg = (
        df_filtered
        .groupBy(window(col("timestamp"), "1 hour").alias("win"))
        .agg(
            avg("temperature").alias("avg_temp"),
            max("temperature").alias("max_temp"),
            min("temperature").alias("min_temp"),
            avg("ph").alias("avg_ph"),
            avg("humidity").alias("avg_humidity"),
            stddev("temperature").alias("stddev_temp"),
        )
        .orderBy("win")
    )

    # “Explode” a coluna window em start/end para facilitar chave no Redis
    df_agg_flat = (
        df_agg
        .withColumn("window_start", col("win.start"))
        .withColumn("window_end",   col("win.end"))
        .drop("win")
    )

    print("\n--- Agregação por Hora (flatten) ---")
    df_agg_flat.show(truncate=False)

    # 7A. Exportação em Parquet (histórico)
    output_path = os.path.join(BASE_DIR, "data_output")
    print(f"Salvando resultados em: {output_path}")
    df_agg_flat.write.mode("overwrite").parquet(output_path)

    # 7B. Exportação para Redis via spark-redis (KPIs em memória)
    # Vamos salvar cada janela como um HASH keyed por "kpi:YYYYMMDDHH"
    # key.column será 'redis_key', criada a partir do window_start
    from pyspark.sql.functions import date_format, concat, lit

    df_kpis_redis = (
        df_agg_flat
        .withColumn(
            "redis_key",
            concat(
                lit("kpi:"),
                date_format(col("window_start"), "yyyyMMddHH")
            )
        )
    )

    print("\n--- Salvando KPIs agregados no Redis (spark-redis) ---")
    (
        df_kpis_redis
        .write
        .format("org.apache.spark.sql.redis")
        .option("table", "shina:kpis")       # prefixo interno de “tabela”
        .option("key.column", "redis_key")  # vira a chave em Redis
        .mode("overwrite")
        .save()
    )

    print("Pipeline concluído com sucesso.")
    spark.stop()

if __name__ == "__main__":
    process_data()
