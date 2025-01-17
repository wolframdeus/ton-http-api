import os
import sys
import json
import asyncio
import base64
import argparse
import os
import inspect
import codecs
import importlib.resources
import warnings
import traceback
import pytimeparse

from functools import partial, wraps
from collections import defaultdict, Counter

from typing import Optional, Union, Dict, Any, List
from pydantic import BaseModel, ValidationError
from fastapi import FastAPI, APIRouter, Depends, Security, Response, Request
from fastapi.params import Body, Query, Param
from fastapi.exceptions import HTTPException, RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import JSONResponse
from fastapi import status
from datetime import datetime, timedelta
from bson import ObjectId
from pymongo import MongoClient

from tvm_valuetypes.cell import deserialize_cell_from_object

from pyTON.models import TonResponse, TonResponseJsonRPC, TonRequestJsonRPC
from config import settings
from pyTON.logging import LoggerAndRateLimitMiddleware, generic_exception_handler, generic_http_exception_handler
from pyTON.multiclient import TonlibMultiClient as TonlibClient
from pyTON.address_utils import detect_address as __detect_address, prepare_address as _prepare_address
from pyTON.wallet_utils import wallets as known_wallets, sha256
from pyTON.utils import TonLibWrongResult
from pyTON.api_key_manager import api_key_manager, check_api_key

from loguru import logger


description = """
This API enables HTTP access to TON blockchain - getting accounts and wallets information, looking up blocks and transactions, sending messages to the blockchain, calling get methods of smart contracts, and more.

In addition to REST API, all methods are available through [JSON-RPC endpoint](#json%20rpc)  with `method` equal to method name and `params` passed as a dictionary.

The response contains a JSON object, which always has a boolean field `ok` and either `error` or `result`. If `ok` equals true, the request was successful and the result of the query can be found in the `result` field. In case of an unsuccessful request, `ok` equals false and the error is explained in the `error`.

API Key should be sent either as `api_key` query parameter or `X-API-Key` header.
"""

tags_metadata = [
    {
        "name": "accounts",
        "description": "Information about accounts.",
    },
    {
        "name": "blocks",
        "description": "Information about blocks.",
    },
    {
        "name": "transactions",
        "description": "Fetching and locating transactions.",
    },
    {
        "name": "run method",
        "description": "Run get method of smart contract.",
    },
    {
        "name": "send",
        "description": "Send data to blockchain.",
    },
    {
        "name": "json rpc",
        "description": "JSON-RPC endpoint.",
    },
]

app = FastAPI(
    title="TON HTTP API",
    description=description,
    version='2.0.0',
    docs_url='/',
    responses={
        422: {'description': 'Validation Error'},
        504: {'description': 'Lite Server Timeout'}
    },
    root_path='/api/v2',
    openapi_tags=tags_metadata,
    dependencies=[Depends(check_api_key)] if settings.ratelimit.enabled else []
)

tonlib = None

@app.on_event("startup")
def startup():
    logger.remove(0)
    logger.add(sys.stdout, level='INFO', enqueue=True)
    logger.add(f'/var/log/main_{os.getpid()}.log', 
               level='INFO', 
               enqueue=True,
               serialize=False,
               backtrace=False,
               rotation='2 weeks')

    loop = asyncio.get_event_loop()

    try:
        with open(settings.pyton.liteserver_config, "r") as f:
            lite_server_config = json.loads(f.read())
    except Exception as e:
        logger.error(f"Can't read provided lite_server_config: {str(e)}")
        raise

    keystore = settings.pyton.keystore
    if not os.path.exists(keystore):
        os.makedirs(keystore)

    # setup tonlib multiclient
    global tonlib
    tonlib = TonlibClient(loop, 
                          lite_server_config, 
                          keystore=keystore, 
                          cdll_path=settings.pyton.cdll)
    tonlib.init_tonlib()

    # setup mongo_database
    if settings.logs.enabled == True:
        with open(settings.logs.mongodb['password_file'], 'r') as f:
            password = f.read()
        client = MongoClient(host=settings.logs.mongodb['host'], 
                             port=settings.logs.mongodb['port'],
                             username=settings.logs.mongodb['username'],
                             password=password)

# Exception handlers

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request, exc):
    return generic_http_exception_handler(exc)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    res = TonResponse(ok=False, error=f"Validation error: {exc}", code=status.HTTP_422_UNPROCESSABLE_ENTITY)
    return JSONResponse(res.dict(exclude_none=True), status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)

@app.exception_handler(asyncio.TimeoutError)
async def timeout_exception_handler(request, exc):
    res = TonResponse(ok=False, error="Liteserver timeout", code=status.HTTP_504_GATEWAY_TIMEOUT)
    return JSONResponse(res.dict(exclude_none=True), status_code=status.HTTP_504_GATEWAY_TIMEOUT)

@app.exception_handler(TonLibWrongResult)
async def tonlib_wront_result_exception_handler(request, exc):
    res = TonResponse(ok=False, error=str(exc), code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    return JSONResponse(res.dict(exclude_none=True), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

@app.exception_handler(Exception)
async def fastapi_generic_exception_handler(request, exc):
    return generic_exception_handler(exc)

# Helper functions

def _detect_address(address):
    try:
        return __detect_address(address)
    except:
        raise HTTPException(status_code=416, detail="Incorrect address")

def prepare_address(address):
    try:
        return _prepare_address(address)
    except:
        raise HTTPException(status_code=416, detail="Incorrect address")

def address_state(account_info):
    if isinstance(account_info.get("code", ""), int) or len(account_info.get("code", "")) == 0:
        if len(account_info.get("frozen_hash", "")) == 0:
            return "uninitialized"
        else:
            return "frozen"
    return "active"

def wrap_result(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        result = await asyncio.wait_for(func(*args, **kwargs), settings.pyton.request_timeout)
        return TonResponse(ok=True, result=result)
    return wrapper

json_rpc_methods = {}

def json_rpc(method):
    def g(func):
        @wraps(func)
        def f(**kwargs):
            sig = inspect.signature(func)
            for k, v in sig.parameters.items():
                # Add function's default value parameters to kwargs.
                if k not in kwargs and v.default is not inspect._empty:
                    default_val = v.default
                    
                    if isinstance(default_val, Param) or isinstance(default_val, Body):
                        if default_val.default == ...:
                            raise TypeError("Non-optional argument expected")
                        kwargs[k] = default_val.default
                    else:
                        kwargs[k] = default_val

                # Some values (e.g. lt, shard) don't fit in json int and can be sent as str.
                # Coerce such str to int.
                if (v.annotation is int or v.annotation is Optional[int]) and type(kwargs[k]) is str:
                    try:
                        kwargs[k] = int(kwargs[k])
                    except ValueError:
                        raise TypeError(f"Can't parse integer in parameter {k}")

            return func(**kwargs)

        json_rpc_methods[method] = f
        return func
    return g

# Endpoints


@app.get('/getAddressInformation', response_model=TonResponse, response_model_exclude_none=True, tags=['accounts'])
@json_rpc('getAddressInformation')
@wrap_result
async def get_address_information(
    address: str = Query(..., description="Identifier of target TON account in any form.")
    ):
    """
    Get basic information about the address: balance, code, data, last_transaction_id.
    """
    address = prepare_address(address)
    result = await tonlib.raw_get_account_state(address)
    result["state"] = address_state(result)
    if "balance" in result and int(result["balance"]) < 0:
        result["balance"] = 0
    return result

@app.get('/getExtendedAddressInformation', response_model=TonResponse, response_model_exclude_none=True, tags=['accounts'])
@json_rpc('getExtendedAddressInformation')
@wrap_result
async def get_extended_address_information(
    address: str = Query(..., description="Identifier of target TON account in any form.")
    ):
    """
    Similar to previous one but tries to parse additional information for known contract types. This method is based on tonlib's function *getAccountState*. For detecting wallets we recommend to use *getWalletInformation*.
    """
    address = prepare_address(address)
    result = await tonlib.generic_get_account_state(address)
    return result

@app.get('/getWalletInformation', response_model=TonResponse, response_model_exclude_none=True, tags=['accounts'])
@json_rpc('getWalletInformation')
@wrap_result
async def get_wallet_information(
    address: str = Query(..., description="Identifier of target TON account in any form.")
    ):
    """
    Retrieve wallet information. This method parses contract state and currently supports more wallet types than getExtendedAddressInformation: simple wallet, standart wallet, v3 wallet, v4 wallet.
    """
    address = prepare_address(address)
    result = await tonlib.raw_get_account_state(address)
    res = {'wallet': False, 'balance': 0, 'account_state': None, 'wallet_type': None, 'seqno': None}
    res["account_state"] = address_state(result)
    res["balance"] = result["balance"] if (result["balance"] and int(result["balance"]) > 0) else 0
    if "last_transaction_id" in result:
        res["last_transaction_id"] = result["last_transaction_id"]
    ci = sha256(result["code"])
    if ci in known_wallets:
        res["wallet"] = True
        wallet_handler = known_wallets[ci]
        res["wallet_type"] = wallet_handler["type"]
        wallet_handler["data_extractor"](res, result)
    return res

@app.get('/getTransactions', response_model=TonResponse, response_model_exclude_none=True, tags=['accounts', 'transactions'])
@json_rpc('getTransactions')
@wrap_result
async def get_transactions(
    address: str = Query(..., description="Identifier of target TON account in any form."), 
    limit: Optional[int] = Query(default=10, description="Maximum number of transactions in response."), 
    lt: Optional[int] = Query(default=None, description="Logical time of transaction to start with, must be sent with *hash*."), 
    hash: Optional[str] = Query(default=None, description="Hash of transaction to start with, in *base64* or *hex* encoding , must be sent with *lt*."), 
    to_lt: Optional[int] = Query(default=0, description="Logical time of transaction to finish with (to get tx from *lt* to *to_lt*)."), 
    archival: bool = Query(default=False, description="By default getTransaction request is processed by any available liteserver. If *archival=true* only liteservers with full history are used.")
    ):
    """
    Get transaction history of a given address.
    """
    address = prepare_address(address)
    return await tonlib.get_transactions(address, from_transaction_lt=lt, from_transaction_hash=hash, to_transaction_lt=to_lt, limit=limit, archival=archival)

@app.get('/getAddressBalance', response_model=TonResponse, response_model_exclude_none=True, tags=['accounts'])
@json_rpc('getAddressBalance')
@wrap_result
async def get_address_balance(
    address: str = Query(..., description="Identifier of target TON account in any form.")
    ):
    """
    Get balance (in nanotons) of a given address.
    """
    address = prepare_address(address)
    result = await tonlib.raw_get_account_state(address)
    if "balance" in result and int(result["balance"]) < 0:
        result["balance"] = 0
    return result["balance"]

@app.get('/getAddressState', response_model=TonResponse, response_model_exclude_none=True, tags=['accounts'])
@json_rpc('getAddressState')
@wrap_result
async def get_address(
    address: str = Query(..., description="Identifier of target TON account in any form.")
    ):
    """
    Get state of a given address. State can be either *unitialized*, *active* or *frozen*.
    """
    address = prepare_address(address)
    result = await tonlib.raw_get_account_state(address)
    return address_state(result)

@app.get('/packAddress', response_model=TonResponse, response_model_exclude_none=True, tags=['accounts'])
@json_rpc('packAddress')
@wrap_result
async def pack_address(
    address: str = Query(..., description="Identifier of target TON account in raw form.", example="0:83DFD552E63729B472FCBCC8C45EBCC6691702558B68EC7527E1BA403A0F31A8")
    ):
    """
    Convert an address from raw to human-readable format.
    """
    return prepare_address(address)

@app.get('/unpackAddress', response_model=TonResponse, response_model_exclude_none=True, tags=['accounts'])
@json_rpc('unpackAddress')
@wrap_result
async def unpack_address(
    address: str = Query(..., description="Identifier of target TON account in user-friendly form", example="EQCD39VS5jcptHL8vMjEXrzGaRcCVYto7HUn4bpAOg8xqB2N")
    ):
    """
    Convert an address from human-readable to raw format.
    """
    return _detect_address(address)["raw_form"]

@app.get('/getMasterchainInfo', response_model=TonResponse, response_model_exclude_none=True, tags=['blocks'])
@json_rpc('getMasterchainInfo')
@wrap_result
async def get_masterchain_info():
    """
    Get up-to-date masterchain state.
    """
    return await tonlib.getMasterchainInfo()

@app.get('/getConsensusBlock', response_model=TonResponse, response_model_exclude_none=True, tags=['blocks'])
@json_rpc('getConsensusBlock')
@wrap_result
async def get_consensus_block():
    """
    Get consensus block and its update timestamp.
    """
    return await tonlib.getConsensusBlock()

@app.get('/lookupBlock', response_model=TonResponse, response_model_exclude_none=True, tags=['blocks'])
@json_rpc('lookupBlock')
@wrap_result
async def lookup_block(
    workchain: int = Query(..., description="Workchain id to look up block in"), 
    shard: int = Query(..., description="Shard id to look up block in"),
    seqno: Optional[int] = Query(None, description="Block's height"),
    lt: Optional[int] = Query(None, description="Block's logical time"), 
    unixtime: Optional[int] = Query(None, description="Block's unixtime")
    ):
    """
    Look up block by either *seqno*, *lt* or *unixtime*.
    """
    return await tonlib.lookupBlock(workchain, shard, seqno, lt, unixtime)

@app.get('/shards', response_model=TonResponse, response_model_exclude_none=True, tags=['blocks'])
@json_rpc('shards')
@wrap_result
async def shards(
    seqno: int = Query(..., description="Masterchain seqno to fetch shards of.")
    ):
    """
    Get shards information.
    """
    return await tonlib.getShards(seqno)

@app.get('/getBlockTransactions', response_model=TonResponse, response_model_exclude_none=True, tags=['blocks','transactions'])
@json_rpc('getBlockTransactions')
@wrap_result
async def get_block_transactions(
    workchain: int, 
    shard: int, 
    seqno: int, 
    root_hash: Optional[str] = None, 
    file_hash: Optional[str] = None, 
    after_lt: Optional[int] = None, 
    after_hash: Optional[str] = None, 
    count: int = 40
    ):
    """
    Get transactions of the given block.
    """
    return await tonlib.getBlockTransactions(workchain, shard, seqno, count, root_hash, file_hash, after_lt, after_hash)

@app.get('/getBlockHeader', response_model=TonResponse, response_model_exclude_none=True, tags=['blocks'])
@json_rpc('getBlockHeader')
@wrap_result
async def get_block_header(
    workchain: int, 
    shard: int, 
    seqno: int, 
    root_hash: Optional[str] = None, 
    file_hash: Optional[str] = None
    ):
    """
    Get metadata of a given block.
    """
    return await tonlib.getBlockHeader(workchain, shard, seqno, root_hash, file_hash)

@app.get('/tryLocateTx', response_model=TonResponse, response_model_exclude_none=True, tags=['transactions'])
@json_rpc('tryLocateTx')
@wrap_result
async def get_try_locate_tx(
    source: str, 
    destination: str, 
    created_lt: int
    ):
    """
    Locate outcoming transaction of *destination* address by incoming message.
    """
    return await tonlib.tryLocateTxByIncomingMessage(source, destination, created_lt)

@app.get('/tryLocateResultTx', response_model=TonResponse, response_model_exclude_none=True, tags=['transactions'])
@json_rpc('tryLocateResultTx')
@wrap_result
async def get_try_locate_result_tx(
    source: str, 
    destination: str, 
    created_lt: int
    ):
    """
    Same as previous. Locate outcoming transaction of *destination* address by incoming message
    """
    return await tonlib.tryLocateTxByIncomingMessage(source, destination, created_lt)

@app.get('/tryLocateSourceTx', response_model=TonResponse, response_model_exclude_none=True, tags=['transactions'])
@json_rpc('tryLocateSourceTx')
@wrap_result
async def get_try_locate_source_tx(
    source: str, 
    destination: str, 
    created_lt: int
    ):
    """
    Locate incoming transaction of *source* address by outcoming message.
    """
    return await tonlib.tryLocateTxByOutcomingMessage(source, destination, created_lt)

@app.get('/detectAddress', response_model=TonResponse, response_model_exclude_none=True, tags=['accounts'])
@json_rpc('detectAddress')
@wrap_result
async def detect_address(
    address: str = Query(..., description="Identifier of target TON account in any form.")
    ):
    """
    Get all possible address forms.
    """
    return _detect_address(address)

@app.post('/sendBoc', response_model=TonResponse, response_model_exclude_none=True, tags=['send'])
@json_rpc('sendBoc')
@wrap_result
async def send_boc(
    boc: str = Body(..., embed=True, description="b64 encoded bag of cells")
    ):
    """
    Send serialized boc file: fully packed and serialized external message to blockchain.
    """
    boc = base64.b64decode(boc)
    return await tonlib.raw_send_message(boc)

@app.post('/sendCellSimple', response_model=TonResponse, response_model_exclude_none=True, include_in_schema=False, tags=['send'])
@json_rpc('sendCellSimple')
@wrap_result
async def send_cell(
    cell: Dict[str, Any] = Body(..., embed=True, description="Cell serialized as object")
    ):
    """
    (Deprecated) Send cell as object: `{"data": {"b64": "...", "len": int }, "refs": [...subcells...]}`, that is fully packed but not serialized external message.
    """
    try:
        cell = deserialize_cell_from_object(cell)
        boc = codecs.encode(cell.serialize_boc(), 'base64')
    except:
        raise HTTPException(status_code=400, detail="Error while parsing cell")
    return await tonlib.raw_send_message(boc)

@app.post('/sendQuery', response_model=TonResponse, response_model_exclude_none=True, tags=['send'])
@json_rpc('sendQuery')
@wrap_result
async def send_query(
    address: str = Body(..., description="Address in any format"), 
    body: str = Body(..., description="b64-encoded boc-serialized cell with message body"), 
    init_code: str = Body(default='', description="b64-encoded boc-serialized cell with init-code"), 
    init_data: str = Body(default='', description="b64-encoded boc-serialized cell with init-data")
    ):
    """
    Send query - unpacked external message. This method takes address, body and init-params (if any), packs it to external message and sends to network. All params should be boc-serialized.
    """
    address = prepare_address(address)
    body = codecs.decode(codecs.encode(body, "utf-8"), 'base64')
    code = codecs.decode(codecs.encode(init_code, "utf-8"), 'base64')
    data = codecs.decode(codecs.encode(init_data, "utf-8"), 'base64')
    return await tonlib.raw_create_and_send_query(address, body, init_code=code, init_data=data)

@app.post('/sendQuerySimple', response_model=TonResponse, response_model_exclude_none=True, include_in_schema=False, tags=['send'])
@json_rpc('sendQuerySimple')
@wrap_result
async def send_query_cell(
    address: str = Body(..., description="Address in any format"), 
    body: str = Body(..., description='Body cell as object: `{"data": {"b64": "...", "len": int }, "refs": [...subcells...]}`'), 
    init_code: Optional[Dict[str, Any]] = Body(default=None, description='init-code cell as object: `{"data": {"b64": "...", "len": int }, "refs": [...subcells...]}`'), 
    init_data: Optional[Dict[str, Any]] = Body(default=None, description='init-data cell as object: `{"data": {"b64": "...", "len": int }, "refs": [...subcells...]}`')
    ):
    """
    (Deprecated) Send query - unpacked external message. This method gets address, body and init-params (if any), packs it to external message and sends to network. Body, init-code and init-data should be passed as objects.
    """
    address = prepare_address(address)
    try:
        body = deserialize_cell_from_object(body).serialize_boc(has_idx=False)
        qcode, qdata = b'', b''
        if init_code is not None:
            qcode = deserialize_cell_from_object(init_code).serialize_boc(has_idx=False)
        if init_data is not None:
            qdata = deserialize_cell_from_object(init_data).serialize_boc(has_idx=False)
    except:
        raise HTTPException(status_code=400, detail="Error while parsing cell object")
    return await tonlib.raw_create_and_send_query(address, body, init_code=qcode, init_data=qdata)

@app.post('/estimateFee', response_model=TonResponse, response_model_exclude_none=True, tags=['send'])
@json_rpc('estimateFee')
@wrap_result
async def estimate_fee(
    address: str = Body(..., description='Address in any format'), 
    body: str = Body(..., description='b64-encoded cell with message body'), 
    init_code: str = Body(default='', description='b64-encoded cell with init-code'), 
    init_data: str = Body(default='', description='b64-encoded cell with init-data'), 
    ignore_chksig: bool = Body(default=True, description='If true during test query processing assume that all chksig operations return True')
    ):
    """
    Estimate fees required for query processing. *body*, *init-code* and *init-data* accepted in serialized format (b64-encoded).
    """
    address = prepare_address(address)
    body = codecs.decode(codecs.encode(body, "utf-8"), 'base64')
    code = codecs.decode(codecs.encode(init_code, "utf-8"), 'base64')
    data = codecs.decode(codecs.encode(init_data, "utf-8"), 'base64')
    return await tonlib.raw_estimate_fees(address, body, init_code=code, init_data=data, ignore_chksig=ignore_chksig)

@app.post('/estimateFeeSimple', response_model=TonResponse, response_model_exclude_none=True, include_in_schema=False, tags=['send'])
@json_rpc('estimateFeeSimple')
@wrap_result
async def estimate_fee_cell(
    address: str = Body(..., description='Address in any format'), 
    body: Dict[str, Any] = Body(..., description='Body cell as object: `{"data": {"b64": "...", "len": int }, "refs": [...subcells...]}`'), 
    init_code: Optional[Dict[str, Any]] = Body(default=None, description='init-code cell as object: `{"data": {"b64": "...", "len": int }, "refs": [...subcells...]}`'), 
    init_data: Optional[Dict[str, Any]] = Body(default=None, description='init-data cell as object: `{"data": {"b64": "...", "len": int }, "refs": [...subcells...]}`'), 
    ignore_chksig: bool = Body(default=True, description='If true during test query processing assume that all chksig operations return True')
    ):
    """
    (Deprecated) Estimate fees required for query processing. *body*, *init-code* and *init-data* accepted in unserialized format (as objects).
    """
    address = prepare_address(address)
    try:
        body = deserialize_cell_from_object(body).serialize_boc(has_idx=False)
        qcode, qdata = b'', b''
        if init_code is not None:
            qcode = deserialize_cell_from_object(init_code).serialize_boc(has_idx=False)
        if init_data is not None:
            qdata = deserialize_cell_from_object(init_data).serialize_boc(has_idx=False)
    except:
        raise HTTPException(status_code=400, detail="Error while parsing cell object")
    return await tonlib.raw_estimate_fees(address, body, init_code=qcode, init_data=qdata, ignore_chksig=ignore_chksig)


if settings.pyton.get_methods:
    @app.post('/runGetMethod', response_model=TonResponse, response_model_exclude_none=True, tags=["run method"])
    @json_rpc('runGetMethod')
    @wrap_result
    async def run_get_method(
        address: str = Body(..., description='Contract address'), 
        method: Union[str, int] = Body(..., description='Method name or method id'), 
        stack: List[List[Any]] = Body(..., description="Array of stack elements: `[['num',3], ['cell', cell_object], ['slice', slice_object]]`")
        ):
        """
        Run get method on smart contract.
        """
        address = prepare_address(address)
        return await tonlib.raw_run_method(address, method, stack)


if settings.pyton.json_rpc:
    @app.post('/jsonRPC', response_model=TonResponseJsonRPC, response_model_exclude_none=True, tags=['json rpc'])
    async def jsonrpc_handler(json_rpc: TonRequestJsonRPC, request: Request, response: Response):
        """
        All methods in the API are available through JSON-RPC protocol ([spec](https://www.jsonrpc.org/specification)). 
        """
        params = json_rpc.params
        method = json_rpc.method
        _id = json_rpc.id

        if not method in json_rpc_methods:
            response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
            return TonResponseJsonRPC(ok=False, error='Unknown method', id=_id)
        handler = json_rpc_methods[method]

        try:
            if 'request' in inspect.signature(handler).parameters.keys():
                result = await handler(request=request, **params)
            else:
                result = await handler(**params)

        except TypeError as e:
            response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
            return TonResponseJsonRPC(ok=False, error=f'TypeError: {e}', id=_id)
        
        return TonResponseJsonRPC(ok=result.ok, result=result.result, error=result.error, code=result.code, id=_id)


app.add_middleware(
    LoggerAndRateLimitMiddleware,
    endpoints=json_rpc_methods.keys()
)