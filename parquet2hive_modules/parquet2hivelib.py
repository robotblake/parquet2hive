import boto3
import botocore
import re
import json
import sys
import struct

from functools32 import lru_cache
from tempfile import NamedTemporaryFile

from thrift.protocol import TCompactProtocol
from thrift.transport import TTransport
from parquet_format.ttypes import FileMetaData

ignore_patterns = [
    r'.*/$', #dirs
    r'.*/_', #temp dirs and files
    r'.*/[^/]*\$folder\$/?' #metadata dirs and files 
]

udf = {}

def get_bash_cmd(dataset, success_only = False, recent_versions = None, version = None):
    m = re.search("s3://([^/]*)/(.*)", dataset)
    bucket_name = m.group(1)
    prefix = m.group(2)

    s3 = boto3.resource('s3')
    bucket = s3.Bucket(bucket_name)
    versions = get_versions(bucket, prefix)

    if version is not None:
        versions = [v for v in versions if v == version]
        if not versions:
            sys.stderr.write("No schemas available with that version")

    bash_cmd, versions_loaded = "", 0
    for version in versions:
        success_exists = False
        version_prefix = prefix + '/' + version + '/'
        dataset_name = prefix.split('/')[-1]

        keys = sorted(bucket.objects.filter(Prefix=version_prefix), key = lambda obj : obj.last_modified, reverse = True)

        for key in keys:
            if ignore_key(key.key):
                continue

            partition = "/".join(key.key.split("/")[:-1])
            if success_only:
                if check_success_exists(s3, bucket.name, partition):
                    success_exists = True
                else:
                    continue
            break

        else:
            if success_only and not success_exists:
                sys.stderr.write("Ignoring dataset missing _SUCCESS file\n")
            else:
                sys.stderr.write("Ignoring empty dataset\n")
            continue

        sys.stderr.write("Analyzing dataset {}, {}\n".format(dataset_name, version))

        schema = read_schema('s3://{}/{}'.format(key.bucket_name, key.key))

        partitions = get_partitioning_fields(key.key[len(prefix):])

        bash_cmd += "hive -hiveconf hive.support.sql11.reserved.keywords=false -e '{}'".format(avro2sql(schema, dataset_name, version, dataset, partitions)) + '\n'
        if versions_loaded == 0:  # Most recent version
            bash_cmd += "hive -e '{}'".format(avro2sql(schema, dataset_name, version, dataset, partitions, with_version=False)) + '\n'

        versions_loaded += 1
        if recent_versions is not None and versions_loaded >= recent_versions:
            break

    return bash_cmd


def read_schema(file_name):
    if file_name.startswith('s3://'):
        # get bucket and key
        bucket, key = file_name[5:].split('/', 1)

        # create s3 obj
        s3res = boto3.resource('s3')
        s3obj = s3res.Object(bucket, key)
        s3obj.load()

        # get footer size
        offset = s3obj.content_length - 8
        response = s3obj.get(Range='bytes={}-'.format(offset))
        footer_size = struct.unpack('<i', response['Body'].read(4))[0]

        # get footer range of file
        offset = s3obj.content_length - 8 - footer_size
        response = s3obj.get(Range='bytes={}-'.format(offset))

        # set fileobj to response body
        fileobj = response['Body']
    else:
        # open file
        fileobj = open(file_name, 'rb')

        # read footer size
        fileobj.seek(-8, 2)
        footer_size = struct.unpack('<i', fileobj.read(4))[0]

        # seek to beginning of footer
        fileobj.seek(-8 - footer_size, 2)

    # read metadata
    transport = TTransport.TFileObjectTransport(fileobj)
    protocol = TCompactProtocol.TCompactProtocol(transport)
    metadata = FileMetaData()
    metadata.read(protocol)

    # close file
    fileobj.close()

    # parse as json and return
    return json.loads(metadata.key_value_metadata[0].value)


def get_versions(bucket, prefix):
    if not prefix.endswith('/'):
        prefix = prefix + '/'

    xs = bucket.meta.client.list_objects(Bucket=bucket.name, Delimiter='/', Prefix=prefix)
    tentative = [ o.get('Prefix') for o in xs.get('CommonPrefixes', []) ]

    versions = []
    for version_prefix in tentative:
        tmp = filter(bool, version_prefix.split("/"))
        if len(tmp) < 2:
            sys.stderr.write("Ignoring incompatible versioning scheme\n")
            continue

        #we don't yet support importing multiple datasets with a single command
        dataset_prefix = '/'.join(tmp[:-1])
        if dataset_prefix != prefix[:-1]:
            sys.stderr.write("Ignoring dataset nested within prefix. To load this dataset, call p2h on it directly: `parquet2hive s3://{}`\n".format(dataset_prefix))
            continue

        version = tmp[-1]
        if not re.match("^v[0-9]+$", version):
            sys.stderr.write("Ignoring incompatible versioning scheme: version must be an integer prefixed with a 'v'\n")
            continue

        versions.append(version)

    return sorted(versions, key = lambda x : int(x[1:]), reverse = True)

@lru_cache(maxsize = 64)
def check_success_exists(s3, bucket, prefix):
    if not prefix.endswith('/'):
        prefix = prefix + '/'

    success_obj_loc = prefix + '_SUCCESS'
    exists = False

    try:
        res = s3.Object(bucket, success_obj_loc).load()
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] == "404":
            exists = False
        else:
            raise e
    else:
        exists = True

    return exists

def ignore_key(key):
    return any( [re.match(pat, key) for pat in ignore_patterns] )

def get_partitioning_fields(prefix):
    return re.findall("([^=/]+)=[^=/]+", prefix)


def avro2sql(avro, name, version, location, partitions, with_version=True):
    fields = [avro2sql_column(field) for field in avro["fields"]]
    fields_decl = ", ".join(fields)

    if partitions:
        columns = ", ".join(["{} string".format(p) for p in partitions])
        partition_decl = " partitioned by ({})".format(columns)
    else:
        partition_decl = ""

    # check for duplicated fields
    field_names = [field["name"] for field in avro["fields"]]
    duplicate_columns = set(field_names) & set(partitions)
    assert not duplicate_columns, "Columns {} are in both the table columns and the partitioning columns; they should only be in one or another".format(", ".join(duplicate_columns))
    table_name = name + "_" + version if with_version else name
    return "drop table if exists {0}; create external table {0}({1}){2} stored as parquet location '\"'{3}/{4}'\"'; msck repair table {0};".format(table_name, fields_decl, partition_decl, location, version)


def avro2sql_column(avro):
    return "`{}` {}".format(avro["name"], transform_type(avro["type"]))


def transform_type(avro):
    is_dict, is_list, is_str = isinstance(avro, dict), isinstance(avro, list), isinstance(avro, str) or isinstance(avro, unicode)

    unchanged_types = ['string', 'int', 'float', 'double', 'boolean', 'date', 'timestamp', 'binary']
    mapped_types = {'integer' : 'int', 'long' : 'bigint'}

    if is_str and avro in unchanged_types:
        sql_type = avro
    elif is_str and avro in mapped_types:
        sql_type = mapped_types[avro]
    elif is_dict and avro["type"] == "map":
        value_type = avro.get("values", avro.get("valueType")) # this can differ depending on the Avro schema version
        sql_type = "map<string,{}>".format(transform_type(value_type))
    elif is_dict and avro["type"] == "array":
        item_type = avro.get("items", avro.get("elementType")) # this can differ depending on the Avro schema version
        sql_type = "array<{}>".format(transform_type(item_type))
    elif is_dict and avro["type"] in ("record", "struct"):
        fields_decl = ", ".join(["`{}`: {}".format(field["name"], transform_type(field["type"])) for field in avro["fields"]])
        sql_type = "struct<{}>".format(fields_decl)
        if avro["type"] == "record":
            udf[avro["name"]] = sql_type 
    elif is_list:
        sql_type = transform_type(avro[0] if avro[1] == "null" else avro[1])
    elif avro in udf:
        sql_type = udf[avro]
    else:
        raise Exception("Unknown type {}".format(avro))

    return sql_type


