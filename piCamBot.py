

import importlib
import inotify.adapters
import json
import logging
import logging.handlers
import os
import shlex
import shutil
import signal
import subprocess
import sys
import telegram
import threading
import time
import traceback
from six.moves import range
from telegram.error import NetworkError, Unauthorized

class piCamBot:
    def __init__(self):
        # identificacao para manter posicao da ultima mensagem
        self.update_id = None
        # configuracao do arquivo config
        self.config = None
        
        self.logger = None
        # checa se há movimentacao e captura imagem
        self.armed = False
        
        self.bot = None
        
        self.GPIO = None

    def run(self):
        
        logFormat = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        self.logger = logging.getLogger(__name__)
        fileHandler = logging.handlers.TimedRotatingFileHandler(filename='picam.log', when='D', backupCount=7)
        fileHandler.setFormatter(logFormat)
        self.logger.addHandler(fileHandler)
        stdoutHandler = logging.StreamHandler(sys.stdout)
        stdoutHandler.setFormatter(logFormat)
        self.logger.addHandler(stdoutHandler)
        self.logger.setLevel(logging.INFO)

        self.logger.info('Starting')

        try:
            self.config = json.load(open('config.json', 'r'))
        except Exception as e:
            self.logger.error(str(e))
            self.logger.error(traceback.format_exc())
            self.logger.error("Could not parse config file")
            sys.exit(1)
        #checa por conflitos em config options
        if self.config['pir']['enable'] and self.config['motion']['enable']:
            self.logger.error('Enabling both PIR and motion based capturing is not supported')
            sys.exit(1)

        
        if self.config['buzzer']['enable'] or self.config['pir']['enable']:
            self.GPIO = importlib.import_module('RPi.GPIO')

        
        signal.signal(signal.SIGHUP, self.signalHandler)
        signal.signal(signal.SIGINT, self.signalHandler)
        signal.signal(signal.SIGQUIT, self.signalHandler)
        signal.signal(signal.SIGTERM, self.signalHandler)

        
        self.armed = self.config['general']['arm']

        self.bot = telegram.Bot(self.config['telegram']['token'])

        # checa permissao de acesso da API
        
        self.logger.info('Esperando pela disponibilidade da rede e API.')
        timeout = self.config['general']['startup_timeout']
        timeout = timeout if timeout > 0 else sys.maxsize
        for i in range(timeout):
            try:
                self.logger.info(self.bot.getMe())
                self.logger.info('Acesso a API permitido!')
                break # success
            except NetworkError as e:
                pass 
            except Exception as e:
                
                self.logger.error(str(e))
                self.logger.error(traceback.format_exc())
                raise
            time.sleep(1)

        # manda mensagem de inicializacao
        for owner_id in self.config['telegram']['owner_ids']:
            try:
                self.bot.sendMessage(chat_id=owner_id, text='Ola, estou de volta!')
            except Exception as e:
                
                self.logger.warn('Nao foi possivel enviar mensagem para %s: %s' % (owner_id, str(e)))

        
        try:
            self.update_id = self.bot.getUpdates()[0].update_id
        except IndexError:
            self.update_id = None

        # configura o disparo do buzzer
        if self.config['buzzer']['enable']:
            gpio = self.config['buzzer']['gpio']
            self.GPIO.setmode(self.GPIO.BOARD)
            self.GPIO.setup(gpio, self.GPIO.OUT)

        threads = []

        # inicia thread no Telegram
        telegram_thread = threading.Thread(target=self.fetchTelegramUpdates, name="Telegram")
        telegram_thread.daemon = True
        telegram_thread.start()
        threads.append(telegram_thread)

        # cria Thread de imagens
        image_watch_thread = threading.Thread(target=self.fetchImageUpdates, name="Image watch")
        image_watch_thread.daemon = True
        image_watch_thread.start()
        threads.append(image_watch_thread)

        
        if self.config['pir']['enable']:
            pir_thread = threading.Thread(target=self.watchPIR, name="PIR")
            pir_thread.daemon = True
            pir_thread.start()
            threads.append(pir_thread)

        while True:
            time.sleep(1)
            
            for thread in threads:
                if thread.isAlive():
                    continue

                
                msg = 'Thread "%s" died, terminating now.' % thread.name
                self.logger.error(msg)
                for owner_id in self.config['telegram']['owner_ids']:
                    try:
                        self.bot.sendMessage(chat_id=owner_id, text=msg)
                    except Exception as e:
                        pass
                sys.exit(1)

    def fetchTelegramUpdates(self):
        self.logger.info('Configurando telegram thread')
        while True:
            try:
                
                for update in self.bot.getUpdates(offset=self.update_id, timeout=10):
                    
                    if not update.message:
                        continue

                    # necessario chat id para enviar qualquer mensagem
                    chat_id = update.message.chat_id
                    self.update_id = update.update_id + 1
                    message = update.message

                    # 
                    if message.from_user.id not in self.config['telegram']['owner_ids']:
                        self.logger.warn('Recebendo mensagem de usuario desconhecido "%s": "%s"' % (message.from_user, message.text))
                        message.reply_text("Nao permitido")
                        continue

                    self.logger.info('Mensagem recebida do usuario "%s": "%s"' % (message.from_user, message.text))
                    self.performCommand(message)
            except NetworkError as e:
                time.sleep(1)
            except Exception as e:
                self.logger.warn(str(e))
                self.logger.warn(traceback.format_exc())
                time.sleep(1)

    def performCommand(self, message):
        cmd = message.text.lower().rstrip()
        if cmd == '/start':
            # ignora comando default start do chatbot
            return
        if cmd == '/arm':
            self.commandArm(message)
        elif cmd == '/disarm':
            self.commandDisarm(message)
        elif cmd == 'kill':
            self.commandKill(message)
        elif cmd == '/status':
            self.commandStatus(message)
        elif cmd == '/capture':
            # caso o software esteja rodando, envia o comando para disparar a câmera
            stopStart = self.isMotionRunning()
            if stopStart:
                self.commandDisarm(message)
            self.commandCapture(message)
            if stopStart:
                self.commandArm(message)
        else:
            self.logger.warn('Comando desconhecido: "%s"' % message.text)

    def commandArm(self, message):
        if self.armed:
            message.reply_text('Dispositivo de captura já habilitado!')
            return

        if not self.config['motion']['enable'] and not self.config['pir']['enable']:
            message.reply_text('Erro!')
            return

        message.reply_text('Habilitando câmera.')

        if self.config['buzzer']['enable']:
            buzzer_sequence = self.config['buzzer']['seq_arm']
            if len(buzzer_sequence) > 0:
                self.playSequence(buzzer_sequence)

        self.armed = True

        if not self.config['motion']['enable']:
            
            return

        # incia sensor de movimento
        if self.isMotionRunning():
            message.reply_text('Iniciando sensor de movimento.')
            return

        args = shlex.split(self.config['motion']['cmd'])
        try:
            subprocess.call(args)
        except Exception as e:
            self.logger.warn(str(e))
            self.logger.warn(traceback.format_exc())
            message.reply_text('Erro: %s' % str(e))
            return

        
        for i in range(10):
            if self.isMotionRunning():
                message.reply_text('Sensor de movimento inicializado.')
                return
            time.sleep(1)
        message.reply_text('Sensor de movimento não está funcionando. Tente novamente.')

    def commandDisarm(self, message):
        if not self.armed:
            message.reply_text('Sensor de movimento desabilitado.')
            return

        message.reply_text('Desabilitando sensor de movimento.')

        if self.config['buzzer']['enable']:
            buzzer_sequence = self.config['buzzer']['seq_disarm']
            if len(buzzer_sequence) > 0:
                self.playSequence(buzzer_sequence)

        self.armed = False

        if not self.config['motion']['enable']:
            
            return

        pid = self.getMotionPID()
        if pid is None:
            message.reply_text('No PID file found.')
            return

        if not os.path.exists('/proc/%s' % pid):
            message.reply_text('Removing PID file.')
            os.remove(self.config['motion']['pid_file'])
            return

        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            
            pass
        
        for i in range(10):
            if not os.path.exists('/proc/%s' % pid):
                message.reply_text('Sensor de presença parou.')
                return
            time.sleep(1)
        
        message.reply_text("Erro.")
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            
            pass

        
        for i in range(10):
            if not os.path.exists('/proc/%s' % pid):
                message.reply_text('Sensor de movimento parou.')
                return
            time.sleep(1)
        message.reply_text('Erro.')

    def commandKill(self, message):
        if not self.config['motion']['enable']:
            message.reply_text('Erro.')
            return
        args = shlex.split('killall -9 %s' % self.config['motion']['kill_name'])
        try:
            subprocess.call(args)
        except Exception as e:
            self.logger.warn(str(e))
            self.logger.warn(traceback.format_exc())
            message.reply_text('Error:: %s' % str(e))
            return
        message.reply_text('Kill.')

    def commandStatus(self, message):
        if not self.armed:
            message.reply_text('Dispositivo de captura não habilitado.')
            return

        image_dir = self.config['general']['image_dir']
        if not os.path.exists(image_dir):
            message.reply_text('Erro:Imagem não disponível!')
            return
     
        if self.config['motion']['enable']:
            # Checa se o sensor está ativo 
            if not self.isMotionRunning():
                message.reply_text('Erro: Câmera está ligada mas há erro de software desconhecido!')
                return
            message.reply_text('Todos os dispositivos estão funcionando corretamente.')
        else:
            message.reply_text('Câmera ligada!')

    def commandCapture(self, message):
        message.reply_text('Capturando imagem, espere um instante...')

        if self.config['buzzer']['enable']:
            buzzer_sequence = self.config['buzzer']['seq_capture']
            if len(buzzer_sequence) > 0:
                self.playSequence(buzzer_sequence)

        capture_file = self.config['capture']['file']
        if sys.version_info[0] == 2: 
            capture_file = capture_file.encode('utf-8')
        if os.path.exists(capture_file):
            os.remove(capture_file)

        args = shlex.split(self.config['capture']['cmd'])
        try:
            subprocess.call(args)
        except Exception as e:
            self.logger.warn(str(e))
            self.logger.warn(traceback.format_exc())
            message.reply_text('Error: Capture failed: %s' % str(e))
            return

        if not os.path.exists(capture_file):
            message.reply_text('Error: Capture file not found: "%s"' % capture_file)
            return
        
        message.reply_photo(photo=open(capture_file, 'rb'))
        if self.config['general']['delete_images']:
            os.remove(capture_file)

    def fetchImageUpdates(self):
        self.logger.info('Setting up image watch thread')

        
        watch_dir = self.config['general']['image_dir']
        #
        if self.config['general']['delete_images']:
            shutil.rmtree(watch_dir, ignore_errors=True)
        if not os.path.exists(watch_dir):
            os.makedirs(watch_dir) 
        notify = inotify.adapters.Inotify()
        notify.add_watch(watch_dir.encode('utf-8'))

       
        for event in notify.event_gen():
            if event is None:
                continue

            (header, type_names, watch_path, filename) = event

            
            matched_types = ['IN_CLOSE_WRITE', 'IN_MOVED_TO']
            if not any(type in type_names for type in matched_types):
                continue

            
            if sys.version_info[0] == 3: 
                watch_path = watch_path.decode()
                filename = filename.decode()
            filepath = ('%s/%s' % (watch_path, filename))

            if not filename.endswith('.jpg'):
                self.logger.info('New non-image file: "%s" - ignored' % filepath)
                continue

            self.logger.info('New image file: "%s"' % filepath)
            if self.armed:
                for owner_id in self.config['telegram']['owner_ids']:
                    try:
                        self.bot.sendPhoto(chat_id=owner_id, caption=filepath, photo=open(filepath, 'rb'))
                    except Exception as e:
                        
                        self.logger.warn('Could not send image to user %s: %s' % (owner_id, str(e)))

            
            if self.config['general']['delete_images']:
                os.remove(filepath)

    def getMotionPID(self):
        pid_file = self.config['motion']['pid_file']
        if not os.path.exists(pid_file):
            return None
        with open(pid_file, 'r') as f:
            pid = f.read().rstrip()
        return int(pid)

    def isMotionRunning(self):
        pid = self.getMotionPID()
        return os.path.exists('/proc/%s' % pid)

    def watchPIR(self):
        self.logger.info('Setting up PIR watch thread')

        if self.config['buzzer']['enable']:
            buzzer_sequence = self.config['buzzer']['seq_motion']

        gpio = self.config['pir']['gpio']
        self.GPIO.setmode(self.GPIO.BOARD)
        self.GPIO.setup(gpio, self.GPIO.IN)
        while True:
            if not self.armed:
                
                time.sleep(1)
                continue

            pir = self.GPIO.input(gpio)
            if pir == 0:
                
                time.sleep(1)
                continue

            self.logger.info('PIR: motion detected')
            if self.config['buzzer']['enable'] and len(buzzer_sequence) > 0:
                self.playSequence(buzzer_sequence)
            args = shlex.split(self.config['pir']['capture_cmd'])

            try:
                subprocess.call(args)
            except Exception as e:
                self.logger.warn(str(e))
                self.logger.warn(traceback.format_exc())
                message.reply_text('Error: Capture failed: %s' % str(e))

    def playSequence(self, sequence):
        gpio = self.config['buzzer']['gpio']
        duration = self.config['buzzer']['duration']
        for i in sequence:
            if i == '1':
                self.GPIO.output(gpio, 1)
            elif i == '0':
                self.GPIO.output(gpio, 0)
            else:
                self.logger.warnprint(': %s', i)
            time.sleep(duration)
        self.GPIO.output(gpio, 0)

    def signalHandler(self, signal, frame):
        # always disable buzzer
        if self.config['buzzer']['enable']:
            gpio = self.config['buzzer']['gpio']
            self.GPIO.output(gpio, 0)
            self.GPIO.cleanup()

        msg = 'Caught signal %d, terminating now.' % signal
        self.logger.error(msg)
        for owner_id in self.config['telegram']['owner_ids']:
            try:
                self.bot.sendMessage(chat_id=owner_id, text=msg)
            except Exception as e:
                pass
        sys.exit(1)

if __name__ == '__main__':
    bot = piCamBot()
    bot.run()
