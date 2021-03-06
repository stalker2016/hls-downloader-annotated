#!/usr/bin/python

import logging
import argparse
import requests
import m3u8
from urllib.parse import urljoin
import grequests
from Crypto.Cipher import AES
from threading import Lock
from time import sleep
import json

tail_mode = False   # 是否进行断点续传
tail_dur = 0    # 如果进行断点续传，该时间点前的视频无需再下载，之后的需要重新下载
pool_size = 5   # ts视频文件块多协程下载时使用的协程池大小
data_timeout = 5    # 单个ts视频文件下载的超时时间
retry_sleep = 3     #单个ts视频文件下载超时后，需要再等待多少秒再进行重试

# logger用于配置和发送日志消息。可以通过logging.getLogger(name)获取logger对象，如果不指定name则返回root对象
logger = logging.getLogger("HLS Downloader")
logger.setLevel(logging.DEBUG)

# handler用于将日志记录发送到合适的目的地
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)

# formatter用于指定日志记录输出的具体格式
formatter = logging.Formatter("%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s]  %(message)s")
ch.setFormatter(formatter)

logger.addHandler(ch)   # 一个logger对象可以通过addHandler方法添加多个handler，每个handler又可以定义不同日志级别，以实现日志分级过滤显示

#-----------------------------创建文件Handler并将其绑定到logger-------------------------
logf = logging.FileHandler("hls.log")
logf.setLevel(logging.DEBUG)
logf.setFormatter(formatter)
logger.addHandler(logf)
#---------------------------------------------------------------------------------------

parser = argparse.ArgumentParser(description='Crawl a HLS Playlist')
parser.add_argument('url', type=str, help='Playlist URL')   # 必须参数，指定m3u8文件的下载链接
parser.add_argument('-f', '--file', type=str, help='Output File')   # 可选参数，输出文件（如果有则将下载得到的所有ts文件写入到它）
parser.add_argument('-k', '--keyfile', type=str, help='Key File')   # 保存着用于解密解压流文件的密钥的文件
parser.add_argument('-d', '--dur', type=int, help='Tail Mode (Time)')   # 用于断点续传，该时间点前的视频无需再下载，之后的需要重新下载
parser.add_argument('-a', '--append', dest='append', action='store_true', help='Append Mode')   # store_true表示如果解析到该项则赋值为True
parser.add_argument('--header', default="", type=str, help='Header (JSON)')     # 网络请求头键值对的JSON对象序列化得到的二进制文件
parser.add_argument('--cookie', default="", type=str, help='Cookie (JSON)')     # cookie键值对的JSON对象序列化得到的二进制文件
args = parser.parse_args()

playlist_url = args.url
logger.info("Playlist URL: " + playlist_url)

control = requests.Session()    # m3u8文件下载会话
data_pool = grequests.Pool(pool_size)   # ts文件下载协程池

# 是否进行断点续传
if args.dur:
    tail_mode = True
    tail_dur = int(args.dur)

# 是否存在指定的输出文件，如果没有则单独下载每一个ts文件；否则讲这些ts文件合并到该输出文件中
if args.file:
    file_mode = True
    out_file = args.file
else:
    file_mode = False

#------------------------读取请求头和Cookie配置文件-----------------------
cookie_file = args.cookie
header_file = args.header
cookie_dict = dict()
header_dict = dict()
try:
    if len(cookie_file) > 0:
        with open(cookie_file, "rb") as cookie_f:
            cookie_json = json.load(cookie_f)
            for ele in cookie_json:
                key = ele["name"]
                val = ele["value"]
                cookie_dict[key] = val

    if len(header_file) > 0:
        with open(header_file, "rb") as header_f:
            header_json = json.load(header_f)
            for ele in header_json:
                header_dict.update(ele)
except IOError as e:
    print(e)
logger.debug(cookie_dict)
logger.debug(header_dict)
#------------------------------------------------------------------------

mpl_res = control.get(playlist_url, cookies=cookie_dict, headers=header_dict)
content = mpl_res.content
playlist_url = mpl_res.url
logger.info("Main Playlist %s ST %d" % (playlist_url, mpl_res.status_code))

#--------------------------根据分辨率或者比特率，解析二级m3u8（如果有的话）---------------------------
content = content.decode('utf8')
variant_m3u8 = m3u8.loads(content)
streams_uri = dict()

# 对于每一个ts视频列表，转化为分辨率-->uri或者比特率-->uri的映射关系，并保存在streams_uri中
for playlist in variant_m3u8.playlists:
    if playlist.stream_info.resolution :
        resolution = int(playlist.stream_info.resolution[1])
        logger.info("Stream at %dp detected!" % resolution)
    else:
        resolution = int(playlist.stream_info.bandwidth)
        logger.info("Stream with bandwidth %d detected!" % resolution)
    streams_uri[resolution] = urljoin(playlist_url, playlist.uri)
#------------------------------------------------------------------------------------------------------

#-----------------------------选取最终要下载的ts视频列表对应的m3u8链接地址-----------------------------
auto_highest = True # 是否自动选取最高画质的那个ts视频列表
stream_res = 0

if auto_highest and len(variant_m3u8.playlists) > 0:    # 如果存在二级m3u8索引
    stream_res = max(streams_uri)
    logger.info("Stream Picked: %dp" % stream_res)
    stream_uri = streams_uri[stream_res]
else: # 如果没有二级m3u8索引，那么就选一级m3u8索引即可（说明没有可选的其他画质）
    stream_uri = playlist_url
logger.info("Chunk List: %s" % (stream_uri))
#------------------------------------------------------------------------------------------------------

old_start = -1
old_end = -1
new_start = -1
new_end = -1

chunk_retry_limit = 10  # 网络请求重试次数上限
chunk_retry = 0     # 网络请求重试计数器
chunk_retry_time = 10   # 每一次请求失败后需等待多少秒后再进行重试

last_write = -1 # 当file_mode为True时，该变量才起作用，记录最后一个写入输出文件的ts文件对应的索引

if file_mode:
    if args.append:     # 将下载得到ts文件逐个追加到指定的输出文件
        out_f = open(out_file, "ab")
    else:               # 首先把输出文件的内容清空，然后再将下载得到ts文件逐个追加到指定的输出文件
        out_f = open(out_file, "wb")
    out_f_lock = Lock()     # 获取文件锁
    fetched_set = set()     # seq存在fetched_set中，则说明索引为seq的ts文件已经下载好了
    fetched_data = dict()   # fetched_data[seq]对应着索引为seq的ts文件的文件内容

error_count = {}

while True:
    try:
        pl_res = control.get(stream_uri, timeout=5, cookies=cookie_dict, headers=header_dict)
    #------------------------------------如果出错则进行重试------------------------------------
    except Exception as e:
        logger.info("Cannot Get Chunklist")
        if chunk_retry < chunk_retry_limit:
            sleep(chunk_retry_time)
            chunk_retry += 1
            continue
        else:
            break
    if not pl_res.status_code == requests.codes.ok:
        logger.info("Cannot Get Chunklist")
        if chunk_retry < chunk_retry_limit:
            sleep(chunk_retry_time)
            chunk_retry += 1
            continue
        else:
            break
    #---------------------------------------------------------------------------------------------
    chunk_retry = 0 # 重置网络请求重试计数器
    content = pl_res.content
    content = content.decode('utf8')
    chunklist = m3u8.loads(content)

    enc = None  # 流的压缩加密方式
    #-------------------------如果流被压缩加密，则获取对应的加密算法、密钥、初始化向量----------------------
    if hasattr(chunklist, "key"):
        enc = chunklist.key
        logger.info("Stream Encrypted with %s!", enc.method)
        if hasattr(enc, "iv"): # 常用于AES加密，iv即为初始化向量
	        enc.iv = enc.iv[2:].decode("hex")
        if hasattr(args, "keyfile"):
            with open(args.keyfile) as f:
                enc.key = f.read()
        else:
            key_uri = urljoin(playlist_url, enc.uri)
            enc.key = control.get(key_uri, cookies=cookie_dict, headers=header_dict).content
    #--------------------------------------------------------------------------------------------------------
    target_dur = chunklist.target_duration
    
    start_seq = chunklist.media_sequence    # 起始ts文件的索引，通常为1（但对于类似于直播有多个m3u8文件的情况就不一样了）
	
    seg_urls = dict()   # seg_urls[seq]代表索引下标seq对应的ts文件下载链接地址

    if start_seq == None:
        logger.warning("Incorrect Chunklist")
        sleep(chunk_retry_time)
        continue

    # 初始化last_write
    if last_write == -1:
        last_write = start_seq - 1

    seq = chunklist.media_sequence	# 起始ts文件的索引，通常为1（但对于类似于直播有多个m3u8文件的情况就不一样了）
    sleep_dur = 0
    updated = False
    list_end = chunklist.is_endlist # 对于直播等场景，还会有下一个m3u8文件

    for segment in chunklist.segments:
        seg_urls[seq] = urljoin(stream_uri, segment.uri)
        logger.info(seg_urls[seq])
        sleep_dur = segment.duration
        seq = seq + 1

    old_start = new_start
    old_end = new_end
    new_start = start_seq   # 该ts文件列表起始ts文件的索引下标
    new_end = seq - 1   # 该ts文件列表结束ts文件的索引下标

    if old_end == -1:   # 这是第一个ts文件列表
        #--------------------如果启用了断点续传的功能，则需要重新设置起始断片的下标索引------------------
        if tail_mode:
            logger.info("Tail Mode")
            if tail_dur > 0:
                logger.info("Tail Time: %d" % (tail_dur))
                tail_size = int(tail_dur / target_dur)
            new_start = new_end - tail_size
            if new_start < start_seq:
                new_start = start_seq
        #-------------------------------------------------------------------------------------------------
        else:
            new_start = start_seq
        last_write = new_start - 1
    else:
        new_start = old_end + 1
    
    segment_reqs = list()

    def decode_and_write(resp, seq, enc):
        global error_count
        global last_write
        global fetched_set
        global fetched_data
        if resp.status_code != 200 or int(resp.headers['content-length']) != len(resp.content):
            raise Exception('Content')

        logger.info("Processing Segment #%d" % (seq))
        out_data = resp.content
        # 如果被加密压缩了，则进行解密解压（AES算法）
        if enc:
            if not enc.iv:
                enc.iv = "0000000000000000"
            dec = AES.new(enc.key, AES.MODE_CBC,enc.iv)
            out_data = dec.decrypt(out_data)

        if file_mode:
            out_f_lock.acquire()    # 因为有可能要进行文件写操作，因此要先上锁
            fetched_set.add(seq)    # 更新已下载ts文件的索引信息
            fetched_data[seq] = out_data    # 更新已下载ts文件的索引信息

            while True:
                # 如果下一个ts文件已经下载好了，就追加到指定输出文件，并清除以节省内存
                if last_write + 1 in fetched_set:
                    last_write = last_write + 1
                    if fetched_data[last_write]:
                        write_data = fetched_data[last_write]
                        logger.debug("Writing %d to %s" % (last_write, out_file));
                        out_f.write(write_data)
                        del fetched_data[last_write]
                    else:
                        logger.debug("Skip writing %d to %s" % (last_write, out_file));
                        del fetched_data[last_write]
                else:
                    break
            out_f_lock.release()

        else:   # 如果没有指定最终输出的文件，则直接把每个ts文件单独下载下来，不做任何其他操作
            filename = "%05d"%(seq) + ".ts"
            logger.debug("Write to %s" % (filename))
            video_f = open(filename, "wb")
            video_f.write(out_data)
            video_f.close()
                
    # 下载一个ts文件，seq为其下标索引，enc为其加密压缩方式
    def get_one(seq, enc):
        global error_count
        global fetched_set
        global fetched_data
        global data_pool
        while True:
            try:
                resp = requests.request('GET', seg_urls[seq], timeout=data_timeout)
                logger.debug(seg_urls[seq])
                decode_and_write(resp, seq, enc)
                break
            except Exception as e:
                print(e)
                logger.info("Content Problem, Retrying for %d" % (seq))
                error_count[seq] = error_count[seq] + 1
                if error_count[seq] > 10:
                    logger.warning("Seq %d Failed" % (seq))
                    if file_mode:
                        fetched_data[seq] = None
                        fetched_set.add(seq)
                    break
                sleep(retry_sleep)

    for seq in range(new_start, new_end + 1):
        error_count[seq] = 0
        data_pool.spawn(get_one,seq,enc)    # 启动协程池进行下载
        updated = True

    sleep_dur = target_dur / 2 # 休眠一段时间再获取下一个m3u8文件

    if list_end:    # 已经加载完了所有ts文件的下载地址
        break

    logger.debug("Sleep for %d secs before reloading" % (sleep_dur))
    sleep(sleep_dur)

logger.info("Stream Ended")
data_pool.join()

if file_mode:
    out_f.close()
