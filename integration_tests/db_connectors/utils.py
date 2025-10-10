import time
import uuid
from dataclasses import dataclass
from typing import Union

import boto3
import mysql.connector
import psycopg2
import requests
from pymongo import MongoClient

import pathway as pw
from pathway.internals import dtype

# from pgvector.psycopg2 import register_vector # FIXME enable once pgvector can be added to env


POSTGRES_DB_HOST = "postgres"
POSTGRES_DB_PORT = 5432
POSTGRES_DB_USER = "user"
POSTGRES_DB_PASSWORD = "password"
POSTGRES_DB_NAME = "tests"
POSTGRES_SETTINGS = {
    "host": POSTGRES_DB_HOST,
    "port": str(POSTGRES_DB_PORT),
    "dbname": POSTGRES_DB_NAME,
    "user": POSTGRES_DB_USER,
    "password": POSTGRES_DB_PASSWORD,
}
PGVECTOR_DB_HOST = "pgvector"
PGVECTOR_SETTINGS = {
    "host": PGVECTOR_DB_HOST,
    "port": str(POSTGRES_DB_PORT),
    "dbname": POSTGRES_DB_NAME,
    "user": POSTGRES_DB_USER,
    "password": POSTGRES_DB_PASSWORD,
}

QUEST_DB_HOST = "questdb"
QUEST_DB_WIRE_PORT = 8812
QUEST_DB_LINE_PORT = 9000
QUEST_DB_NAME = "qdb"
QUEST_DB_USER = "admin"
QUEST_DB_PASSWORD = "quest"

MONGODB_HOST_WITH_PORT = "mongodb:27017"
MONGODB_CONNECTION_STRING = f"mongodb://{MONGODB_HOST_WITH_PORT}/?replicaSet=rs0"
MONGODB_BASE_NAME = "tests"

KAFKA_BOOTSTRAP_SERVERS = "kafka:9092"
KAFKA_SETTINGS = {
    "bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
    "security.protocol": "plaintext",
    "group.id": "0",
    "session.timeout.ms": "6000",
    "auto.offset.reset": "earliest",
}

DEBEZIUM_CONNECTOR_URL = "http://debezium:8083/connectors"

MYSQL_DB_HOST = "mysql"
MYSQL_DB_PORT = 3306
MYSQL_DB_NAME = "testdb"
MYSQL_DB_USER = "testuser"
MYSQL_DB_PASSWORD = "testpass"
MYSQL_CONNECTION_STRING = (
    f"mysql://{MYSQL_DB_USER}:{MYSQL_DB_PASSWORD}"
    + f"@{MYSQL_DB_HOST}:{MYSQL_DB_PORT}/{MYSQL_DB_NAME}"
)


@dataclass(frozen=True)
class ColumnProperties:
    type_name: str
    is_nullable: bool


class SimpleObject:
    def __init__(self, a):
        self.a = a

    def __eq__(self, other):
        return self.a == other.a


class WireProtocolSupporterContext:

    def __init__(
        self, *, host: str, port: int, database: str, user: str, password: str
    ):
        self.connection = psycopg2.connect(
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
        )
        self.connection.autocommit = True
        self.cursor = self.connection.cursor()

    def get_table_schema(
        self, table_name: str, schema: str = "public"
    ) -> dict[str, ColumnProperties]:
        query = """
            SELECT
                column_name,
                data_type,
                is_nullable
            FROM information_schema.columns
            WHERE table_name = %s AND table_schema = %s
            ORDER BY ordinal_position;
        """
        self.cursor.execute(query, (table_name, schema))
        rows = self.cursor.fetchall()

        schema_props = {}
        for column_name, type_name, is_nullable in rows:
            assert is_nullable in ("YES", "NO")
            schema_props[column_name] = ColumnProperties(
                type_name.lower(), is_nullable == "YES"
            )
        return schema_props

    def insert_row(
        self, table_name: str, values: dict[str, int | bool | str | float]
    ) -> None:
        field_names = []
        field_values = []
        for key, value in values.items():
            field_names.append(key)
            if isinstance(value, str):
                field_values.append(f"'{value}'")
            elif value is True:
                field_values.append("'t'")
            elif value is False:
                field_values.append("'f'")
            else:
                field_values.append(str(value))
        condition = f'INSERT INTO {table_name} ({",".join(field_names)}) VALUES ({",".join(field_values)})'
        print(f"Inserting a row: {condition}")
        self.cursor.execute(condition)

    def create_table(self, schema: type[pw.Schema], *, add_special_fields: bool) -> str:
        table_name = self.random_table_name()

        primary_key_found = False
        fields = []
        for field_name, field_schema in schema.columns().items():
            parts = [field_name]
            field_type = field_schema.dtype
            if field_type == dtype.STR:
                parts.append("TEXT")
            elif field_type == dtype.INT:
                parts.append("BIGINT")
            elif field_type == dtype.FLOAT:
                parts.append("DOUBLE PRECISION")
            elif field_type == dtype.BOOL:
                parts.append("BOOLEAN")
            elif isinstance(field_type, dtype.Array) and "_vector" in field_name:
                # hack to create an array with a specific type
                parts.append("VECTOR")
            elif isinstance(field_type, dtype.Array) and "_halfvec" in field_name:
                # hack to create an array with a specific type
                parts.append("HALFVEC")
            else:
                raise RuntimeError(f"This test doesn't support field type {field_type}")
            if field_schema.primary_key:
                assert (
                    not primary_key_found
                ), "This test only supports simple primary keys"
                primary_key_found = True
                parts.append("PRIMARY KEY NOT NULL")
            fields.append(" ".join(parts))

        if add_special_fields:
            fields.append("time BIGINT NOT NULL")
            fields.append("diff BIGINT NOT NULL")

        self.cursor.execute(
            f'CREATE TABLE IF NOT EXISTS {table_name} ({",".join(fields)})'
        )

        return table_name

    def get_table_contents(
        self,
        table_name: str,
        column_names: list[str],
        sort_by: str | tuple | None = None,
    ) -> list[dict[str, str | int | bool | float]]:
        select_query = f'SELECT {",".join(column_names)} FROM {table_name};'
        self.cursor.execute(select_query)
        rows = self.cursor.fetchall()
        result = []
        for row in rows:
            row_map = {}
            for name, value in zip(column_names, row):
                row_map[name] = value
            result.append(row_map)
        if sort_by is not None:
            if isinstance(sort_by, tuple):
                result.sort(key=lambda item: tuple(item[key] for key in sort_by))
            else:
                result.sort(key=lambda item: item[sort_by])
        return result

    def random_table_name(self) -> str:
        return f'wire_{str(uuid.uuid4()).replace("-", "")}'


class PostgresContext(WireProtocolSupporterContext):

    def __init__(self):
        super().__init__(
            host=POSTGRES_DB_HOST,
            port=POSTGRES_DB_PORT,
            database=POSTGRES_DB_NAME,
            user=POSTGRES_DB_USER,
            password=POSTGRES_DB_PASSWORD,
        )


class PgvectorContext(WireProtocolSupporterContext):

    def __init__(self):
        super().__init__(
            host=PGVECTOR_DB_HOST,
            port=POSTGRES_DB_PORT,
            database=POSTGRES_DB_NAME,
            user=POSTGRES_DB_USER,
            password=POSTGRES_DB_PASSWORD,
        )
        self.cursor.execute("CREATE EXTENSION vector")
        # register_vector(self.connection) # FIXME


class QuestDBContext(WireProtocolSupporterContext):

    def __init__(self):
        super().__init__(
            host=QUEST_DB_HOST,
            port=QUEST_DB_WIRE_PORT,
            database=QUEST_DB_NAME,
            user=QUEST_DB_USER,
            password=QUEST_DB_PASSWORD,
        )


class MongoDBContext:
    client: MongoClient

    def __init__(self):
        self.client = MongoClient(MONGODB_CONNECTION_STRING)

    def generate_collection_name(self) -> str:
        table_name = f'mongodb_{str(uuid.uuid4()).replace("-", "")}'
        return table_name

    def get_collection(
        self, collection_name: str, field_names: list[str]
    ) -> list[dict[str, str | int | bool | float]]:
        db = self.client[MONGODB_BASE_NAME]
        collection = db[collection_name]
        data = collection.find()
        result = []
        for document in data:
            entry = {}
            for field_name in field_names:
                entry[field_name] = document[field_name]
            result.append(entry)
        return result

    def insert_document(
        self, collection_name: str, document: dict[str, int | bool | str | float]
    ) -> None:
        db = self.client[MONGODB_BASE_NAME]
        collection = db[collection_name]
        collection.insert_one(document)


class DebeziumContext:

    def _register_connector(self, payload: dict, result_on_ok: str) -> str:
        for _ in range(300):
            try:
                r = requests.post(DEBEZIUM_CONNECTOR_URL, timeout=60, json=payload)
                is_ok = r.status_code // 100 == 2
            except Exception as e:
                print(f"Debezium is not ready to register connector yet: {e}")
                time.sleep(1.0)
                continue
            if is_ok:
                return result_on_ok
            else:
                print(
                    f"Debezium is not ready to register connector yet. Code: {r.status_code}. Text: {r.text}"
                )
                time.sleep(1.0)
        raise RuntimeError("Failed to register Debezium connector")

    def register_mongodb(self) -> str:
        connector_id = str(uuid.uuid4()).replace("-", "")
        payload = {
            "name": f"values-connector-{connector_id}",
            "config": {
                "connector.class": "io.debezium.connector.mongodb.MongoDbConnector",
                "mongodb.hosts": f"rs0/{MONGODB_HOST_WITH_PORT}",
                "mongodb.name": f"{connector_id}",
                "database.include.list": MONGODB_BASE_NAME,
                "database.history.kafka.bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
                "database.history.kafka.topic": "dbhistory.mongo",
            },
        }
        return self._register_connector(payload, f"{connector_id}.{MONGODB_BASE_NAME}.")

    def register_postgres(self, table_name: str) -> str:
        connector_id = str(uuid.uuid4()).replace("-", "")
        payload = {
            "name": f"values-connector-{connector_id}",
            "config": {
                "connector.class": "io.debezium.connector.postgresql.PostgresConnector",
                "plugin.name": "pgoutput",
                "database.hostname": POSTGRES_DB_HOST,
                "database.port": str(POSTGRES_DB_PORT),
                "database.user": str(POSTGRES_DB_USER),
                "database.password": str(POSTGRES_DB_PASSWORD),
                "database.dbname": str(POSTGRES_DB_NAME),
                "database.server.name": connector_id,
                "table.include.list": f"public.{table_name}",
                "database.history.kafka.bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
            },
        }
        return self._register_connector(payload, f"{connector_id}.public.{table_name}")


class DynamoDBContext:

    def __init__(self):
        self.dynamodb = boto3.resource("dynamodb", region_name="us-west-2")

    def get_table_contents(self, table_name: str) -> list[dict]:
        table = self.dynamodb.Table(table_name)
        response = table.scan()
        data = response["Items"]

        while "LastEvaluatedKey" in response:
            response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
            data.extend(response["Items"])

        return data

    def generate_table_name(self) -> str:
        return "table" + str(uuid.uuid4())


class MySQLContext:
    def __init__(self):
        self.connection = mysql.connector.connect(
            host=MYSQL_DB_HOST,
            port=MYSQL_DB_PORT,
            database=MYSQL_DB_NAME,
            user=MYSQL_DB_USER,
            password=MYSQL_DB_PASSWORD,
            autocommit=True,
        )
        self.cursor = self.connection.cursor()

    def get_table_schema(self, table_name: str) -> dict[str, ColumnProperties]:
        query = """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = %s AND table_schema = %s
            ORDER BY ordinal_position;
        """
        self.cursor.execute(query, (table_name, self.connection.database))
        rows = self.cursor.fetchall()

        schema_props = {}
        for column_name, type_name, is_nullable in rows:
            schema_props[column_name] = ColumnProperties(
                type_name.lower(), is_nullable.upper() == "YES"
            )
        return schema_props

    def insert_row(
        self, table_name: str, values: dict[str, Union[int, bool, str, float]]
    ) -> None:
        field_names = list(values.keys())
        placeholders = ", ".join(["%s"] * len(values))
        query = f"INSERT INTO {table_name} ({','.join(field_names)}) VALUES ({placeholders})"
        print(f"Inserting a row: {query}")
        self.cursor.execute(query, tuple(values.values()))

    def create_table(self, schema: type[pw.Schema], *, add_special_fields: bool) -> str:
        table_name = self.random_table_name()

        primary_key_found = False
        fields = []
        for field_name, field_schema in schema.columns().items():
            parts = [f"`{field_name}`"]
            field_type = field_schema.dtype
            if field_type == dtype.STR:
                parts.append("VARCHAR(255)")
            elif field_type == dtype.INT:
                parts.append("BIGINT")
            elif field_type == dtype.FLOAT:
                parts.append("DOUBLE")
            elif field_type == dtype.BOOL:
                parts.append("BOOLEAN")
            else:
                raise RuntimeError(f"Unsupported field type {field_type}")
            if field_schema.primary_key:
                if primary_key_found:
                    raise AssertionError("Only single primary key supported")
                primary_key_found = True
                parts.append("PRIMARY KEY NOT NULL")
            fields.append(" ".join(parts))

        if add_special_fields:
            fields.append("`time` BIGINT NOT NULL")
            fields.append("`diff` BIGINT NOT NULL")

        create_sql = f"CREATE TABLE IF NOT EXISTS {table_name} ({','.join(fields)})"
        self.cursor.execute(create_sql)
        return table_name

    def get_table_contents(
        self,
        table_name: str,
        column_names: list[str],
        sort_by: Union[str, tuple, None] = None,
    ) -> list[dict[str, Union[str, int, bool, float]]]:
        select_query = f"SELECT {','.join(column_names)} FROM {table_name};"
        self.cursor.execute(select_query)
        rows = self.cursor.fetchall()
        result = []
        for row in rows:
            row_map = dict(zip(column_names, row))
            result.append(row_map)
        if sort_by is not None:
            if isinstance(sort_by, tuple):
                result.sort(key=lambda item: tuple(item[key] for key in sort_by))
            else:
                result.sort(key=lambda item: item[sort_by])
        return result

    def random_table_name(self) -> str:
        return f"mysql_{uuid.uuid4().hex}"


class EntryCountChecker:

    def __init__(
        self,
        n_expected_entries: int,
        db_context: DynamoDBContext | WireProtocolSupporterContext,
        **get_table_contents_kwargs,
    ):
        self.n_expected_entries = n_expected_entries
        self.db_context = db_context
        self.get_table_contents_kwargs = get_table_contents_kwargs

    def __call__(self) -> bool:
        try:
            table_contents = self.db_context.get_table_contents(
                **self.get_table_contents_kwargs
            )
        except Exception:
            return False
        return len(table_contents) == self.n_expected_entries
