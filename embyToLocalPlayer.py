import json
import os.path
import re
import signal
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from configparser import ConfigParser
from html.parser import HTMLParser
from http.server import HTTPServer, BaseHTTPRequestHandler

from python_mpv_jsonipc import MPV


class PlayerRunningState(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)

    def run(self):
        global player_is_running
        player_is_running = True
        time.sleep(0.1)
        player_is_running = False


class _RequestHandler(BaseHTTPRequestHandler):
    def _set_headers(self):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get('content-length'))
        data = json.loads(self.rfile.read(length))
        self._set_headers()
        self.wfile.write(json.dumps({'success': True}).encode('utf-8'))
        if player_is_running:
            log('reject post when running')
            return
        if 'embyToLocalPlayer' in self.path and not player_is_running:
            emby_to_local_player(data)
        elif 'openFolder' in self.path:
            open_local_folder(data)
        elif 'playMediaFile' in self.path:
            play_media_file(data)
        else:
            log(self.path, ' not allow')
            return json.dumps({'success': True}).encode('utf-8')


def log(*args):
    if not enable_log:
        return
    log_str = f'{time.ctime()} {str(args)}\n'
    if print_only:
        print(log_str, end='')
        return
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write(log_str)


def open_local_folder(data):
    path = data['info'][0]['content_path']
    translate_path = get_player_and_replace_path(path)[1]
    explore(translate_path)
    log('open folder', translate_path)


def play_media_file(data):
    save_path = data['info'][0]['save_path']
    big_file = sorted(data['file'], key=lambda i: i['size'], reverse=True)[0]['name']
    path = os.path.join(save_path, big_file)
    cmd = get_player_and_replace_path(path)
    player_path_lower = cmd[0].lower()
    if 'mpv' in player_path_lower:
        start_mpv_player(cmd, get_stop_sec=False)
        return
    subprocess.run(cmd)


def explore(path):
    if os.name != 'nt':
        log('open folder only work in windows')
    # filebrowser = os.path.join(os.getenv('WINDIR'), 'explorer.exe')
    path = os.path.normpath(path)
    # cmd = [filebrowser, path] if os.path.isdir(path) else [filebrowser, '/select,', path]
    cmd = f'explorer "{path}"' if os.path.isdir(path) else f'explorer /select, "{path}"'
    os.system(cmd)


def get_player_and_replace_path(media_path):
    config = ConfigParser()
    config.read(ini, encoding='utf-8')
    player = config['emby']['player']
    exe = config['exe'][player]
    log(media_path, 'raw')
    if 'src' in config and 'dst' in config and not media_path.startswith('http'):
        src = config['src']
        dst = config['dst']
        # 貌似是有序字典
        for k, src_prefix in src.items():
            if src_prefix in media_path:
                dst_prefix = dst[k]
                tmp_path = media_path.replace(src_prefix, dst_prefix, 1)
                if os.path.exists(tmp_path):
                    media_path = tmp_path
                    break
    result = [exe, media_path]
    log(result, 'cmd')
    return result


def requests_urllib(host, params=None, _json=None, decode=False, timeout=2.0):
    _json = json.dumps(_json).encode('utf-8') if _json else None
    params = urllib.parse.urlencode(params) if params else None
    host = host + '?' + params if params else host
    req = urllib.request.Request(host)
    if _json:
        req.add_header('Content-Type', 'application/json; charset=utf-8')
        response = urllib.request.urlopen(req, _json, timeout=timeout)
    else:
        response = urllib.request.urlopen(req, timeout=timeout)
    if decode:
        return response.read().decode()


def active_window_by_pid(pid, scrip_name='active_video_player'):
    for script_type in '.exe', '.ahk':
        script_path = os.path.join(cwd, f'{scrip_name}{script_type}')
        if os.path.exists(script_path) and os.name == 'nt':
            log(script_path)
            subprocess.run([script_path, str(pid)], shell=True)
            return


def unparse_stream_mkv_url(scheme, netloc, item_id, api_key, media_source_id):
    params = {
        # 'DeviceId': '30477019-ea16-490f-a915-f544f84a7b10',
        'MediaSourceId': media_source_id,
        'Static': 'true',
        # 'PlaySessionId': '1fbf2f87976c4b1a8f7cee0c6875d60f',
        'api_key': api_key,
    }
    path = f'/emby/videos/{item_id}/stream.mkv'
    query = urllib.parse.urlencode(params, doseq=True)
    '(addressing scheme, network location, path, params='', query, fragment identifier='')'
    url = urllib.parse.urlunparse((scheme, netloc, path, '', query, ''))
    return url


def unparse_subtitle_url(scheme, netloc, item_id, api_key, media_source_id, sub_index):
    url = f'{scheme}://{netloc}/emby/Videos/{item_id}/{media_source_id}' \
          f'/Subtitles/{sub_index}/Stream.srt?api_key={api_key}'
    return url


def change_emby_play_position(scheme, netloc, item_id, api_key, stop_sec, play_session_id, device_id):
    ticks = stop_sec * 10 ** 7
    requests_urllib(f'{scheme}://{netloc}/emby/Sessions/Playing',
                    params={
                        'X-Emby-Token': api_key,
                        'X-Emby-Device-Id': device_id,
                    },
                    _json={
                        'ItemId': item_id,
                        'PlaySessionId': play_session_id,
                    })
    requests_urllib(f'{scheme}://{netloc}/emby/Sessions/Playing/Stopped',
                    params={
                        'X-Emby-Token': api_key,
                        'X-Emby-Device-Id': device_id,
                    },
                    _json={
                        'PositionTicks': ticks,
                        'ItemId': item_id,
                        'PlaySessionId': play_session_id,
                        # 'PlaylistIndex': 0,
                        # 'PlaybackRate': 1,
                        # 'PlaylistLength': 1,
                    })


def start_mpv_player(cmd, start_sec=None, sub_file=None, media_title=None, get_stop_sec=True):
    pipe_name = 'embyToMpv'
    cmd_pipe = fr'\\.\pipe\{pipe_name}' if os.name == 'nt' else f'/tmp/{pipe_name}'
    pipe_name = pipe_name if os.name == 'nt' else cmd_pipe
    # cmd.append(f'--http-proxy=http://127.0.0.1:7890')
    if sub_file:
        cmd.append(f'--sub-file={sub_file}')
    if media_title:
        cmd.append(f'--force-media-title={media_title}')
        cmd.append(f'--osd-playing-msg={media_title}')
    else:
        cmd.append('--osd-playing-msg=${path}')
    if start_sec is not None:
        cmd.append(f'--start={start_sec}')
    cmd.append(fr'--input-ipc-server={cmd_pipe}')

    mpv_proc = subprocess.Popen(cmd, shell=False, stdout=subprocess.PIPE)
    active_window_by_pid(mpv_proc.pid)

    if not get_stop_sec:
        return

    try:
        time.sleep(0.1)
        mpv = MPV(start_mpv=False, ipc_socket=pipe_name)
    except Exception as e:
        log(e)
        time.sleep(1)
        mpv = MPV(start_mpv=False, ipc_socket=pipe_name)

    stop_sec = 0
    while True:
        try:
            _stop_sec = mpv.command('get_property', 'time-pos')
            if not _stop_sec:
                print('.', end='')
            else:
                stop_sec = _stop_sec
            time.sleep(0.5)
        except Exception:
            break
    if stop_sec:
        stop_sec = int(stop_sec) - 2 if int(stop_sec) > 5 else int(stop_sec)
    else:
        stop_sec = int(start_sec)
    return stop_sec


class MpcHTMLParser(HTMLParser):
    id_value_dict = {}
    _id = None

    def handle_starttag(self, tag: str, attrs: list[tuple]) -> None:
        if attrs and attrs[0][0] == 'id':
            self._id = attrs[0][1]

    def handle_data(self, data):
        if self._id is not None:
            data = int(data) if data.isdigit() else data.strip()
            self.id_value_dict[self._id] = data
            self._id = None


def mpc_stop_sec():
    url = 'http://localhost:13579/variables.html'
    parser = MpcHTMLParser()
    stop_sec = None
    stack = [None, None]
    first_time = True
    while True:
        try:
            time_out = 1 if first_time else 0.2
            first_time = False
            context = requests_urllib(url, decode=True, timeout=time_out)
            parser.feed(context)
            data = parser.id_value_dict
            position = data['position'] // 1000
            stop_sec = position if data['state'] != '-1' else stop_sec
            stack.pop(0)
            stack.append(stop_sec)
        except Exception:
            log('final stop', stack[-2], stack)
            # 播放器关闭时，webui 可能返回 0
            return stack[-2]
        time.sleep(0.3)


def start_mpc_player(cmd, start_sec=None, sub_file=None, media_title=None, get_stop_sec=True):
    if sub_file:
        # '/dub "伴音名"	载入额外的音频文件'
        cmd += ['/sub', f'"{sub_file}"']
    if start_sec is not None:
        cmd += ['/start', f'"{int(start_sec * 1000)}"']
    if media_title:
        pass
    cmd[1] = f'"{cmd[1]}"'
    cmd += ['/fullscreen', '/play', '/close']
    log(cmd)
    mpv_proc = subprocess.Popen(cmd, shell=False)
    active_window_by_pid(mpv_proc.pid)
    if not get_stop_sec:
        return

    stop_sec = mpc_stop_sec()
    if stop_sec is not None:
        stop_sec = int(stop_sec) - 2 if int(stop_sec) > 5 else int(stop_sec)
    else:
        stop_sec = int(start_sec)
    return stop_sec


def emby_to_local_player(receive_info):
    mount_disk_mode = True if receive_info['mountDiskEnable'] == 'true' else False
    url = urllib.parse.urlparse(receive_info['playbackUrl'])
    query = dict(urllib.parse.parse_qsl(url.query))
    query: dict
    item_id = [i for i in url.path.split('/') if i.isdigit()][0]
    media_source_id = query.get('MediaSourceId')
    api_key = query['X-Emby-Token']
    netloc = url.netloc
    scheme = url.scheme
    device_id = query['X-Emby-Device-Id']
    sub_index = query.get('SubtitleStreamIndex')

    data = receive_info['playbackData']
    media_sources = data['MediaSources']
    play_session_id = data['PlaySessionId']
    if media_source_id:
        file_path = [i['Path'] for i in media_sources if i['Id'] == media_source_id][0]
    else:
        file_path = media_sources[0]['Path']
        media_source_id = media_sources[0]['Id']

    stream_mkv_url = unparse_stream_mkv_url(scheme=scheme, netloc=netloc, item_id=item_id,
                                            api_key=api_key, media_source_id=media_source_id)
    sub_file = unparse_subtitle_url(scheme=scheme, netloc=netloc, item_id=item_id,
                                    api_key=api_key, media_source_id=media_source_id,
                                    sub_index=sub_index
                                    ) if sub_index else None  # 选择外挂字幕
    media_path = file_path if mount_disk_mode else stream_mkv_url
    media_title = os.path.basename(file_path) if not mount_disk_mode else None  # 播放http时覆盖标题

    seek = query['StartTimeTicks']
    start_sec = int(seek) / (10 ** 7) if seek else 0
    cmd = get_player_and_replace_path(media_path)
    player_path_lower = cmd[0].lower()
    # 播放器特殊处理
    if 'mpv' in player_path_lower or 'mpc' in player_path_lower:
        player_function = start_mpv_player if 'mpv' in player_path_lower else start_mpc_player
        stop_sec = player_function(cmd=cmd, start_sec=start_sec, sub_file=sub_file, media_title=media_title)
        log('stop_sec', stop_sec)
        change_emby_play_position(
            scheme=scheme, netloc=netloc, item_id=item_id, api_key=api_key, stop_sec=stop_sec,
            play_session_id=play_session_id, device_id=device_id)
    else:
        if 'potplayer' in player_path_lower and sub_file:
            cmd.append(f'/sub={sub_file}')
        elif 'vlc' in player_path_lower:
            # '--sub-file=<字符串> --input-title-format=<字符串>'
            if mount_disk_mode:
                cmd[1] = f'file:///{cmd[1]}'
            else:
                cmd.append(f'--input-title-format={media_title}')
                # cmd.append(f'--sub-file={sub_file}')  # vlc不支持http字幕
        log(cmd)
        player = subprocess.Popen(cmd, shell=False, stdout=subprocess.PIPE)
        active_window_by_pid(player.pid)
    # set running flag to drop stuck requests
    PlayerRunningState().start()


def list_pid_and_cmd(name_re='.') -> list:
    cmd = 'Get-WmiObject Win32_Process | Select ProcessId,CommandLine | ConvertTo-Json'
    proc = subprocess.run(['powershell', '-Command', cmd], capture_output=True, encoding='gbk')
    if proc.returncode != 0:
        return []
    stdout = [(i['ProcessId'], i['CommandLine']) for i in json.loads(proc.stdout)
              if i['ProcessId'] and i['CommandLine']]
    result = [(pid, _cmd) for (pid, _cmd) in stdout if re.search(name_re, _cmd)]
    return result


def kill_multi_process(name_re):
    if os.name != 'nt':
        return
    my_pid = os.getpid()
    pid_cmd = list_pid_and_cmd(name_re)
    for pid, _ in pid_cmd:
        if pid != my_pid:
            os.kill(pid, signal.SIGABRT)


def run_server():
    server_address = ('127.0.0.1', 58000)
    httpd = HTTPServer(server_address, _RequestHandler)
    print('serving at %s:%d' % server_address, file_name)
    httpd.serve_forever()


if __name__ == '__main__':
    enable_log = True
    print_only = True
    cwd = os.path.dirname(__file__)
    file_name = os.path.basename(__file__)[:-3]
    ini = os.path.join(cwd, f'{file_name}.ini')
    log_path = os.path.join(cwd, f'{file_name}.log')
    player_is_running = False
    kill_multi_process(name_re=f'({file_name}.py' +
                       r'|active_video_player|mpv.*exe|mpc-.*exe)')
    log(__file__)
    run_server()
