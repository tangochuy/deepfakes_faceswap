import matplotlib
import sys

from contextlib import redirect_stdout
from io import StringIO
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from os import environ, kill, path
from queue import Queue
from subprocess import Popen, PIPE, STDOUT, TimeoutExpired
from threading import Thread

from lib.cli import FullPaths
from lib.Serializer import JSONSerializer

PATHSCRIPT = path.realpath(path.dirname(sys.argv[0]))

# Users without tkinter distribution packages or without an X-Console will error out
# importing tkinter. Therefore run a check on these and only import if required 
tk = None
ttk = None
filedialog = None

def import_tkinter(command):
    ''' Perform checks when importing tkinter module to ensure that GUI will load '''
    global tk
    global ttk
    global filedialog
    try:
        import tkinter
        from tkinter import ttk
        from tkinter import filedialog
        tk = tkinter
    except ImportError:
        if 'gui' in command:
            print(  'It looks like TkInter isn''t installed for your OS, so the GUI has been '
                    'disabled. To enable the GUI please install the TkInter application.\n'
                    'You can try:\n'
                    '  Windows/macOS:      Install ActiveTcl Community Edition from '
                    'www.activestate.com\n'
                    '  Ubuntu/Mint/Debian: sudo apt install python3-tk\n'
                    '  Arch:               sudo pacman -S tk\n'
                    '  CentOS/Redhat:      sudo yum install tkinter\n'
                    '  Fedora:             sudo dnf install python3-tkinter\n')
        return False
    return True    

def check_display(command):
    # Check whether there is a display to output the GUI '''
    if not environ.get('DISPLAY', None):
        if 'gui' in command:
            print ('Could not detect a display. The GUI has been disabled')
        return False
    return True

class Utils(object):
    ''' Inter-class object holding items that are required across classes '''
    def __init__(self, options):
        self.opts = options

        self.icofolder = None
        self.icoload = None
        self.icosave = None
        self.icoreset = None
        self.icoclear = None
        
        self.console = None
        self.helptext = None
        self.actiontext = {}
        self.statustext = None

        self.serializer = JSONSerializer
        self.filetypes=(('Faceswap files', '*.fsw'),  ('All files', '*.*'))
        
        self.runningtask = False
        self.task = FaceswapControl(self)

    def init_tk(self):
        ''' TK System must be on prior to setting tk variables, so initialised from GUI '''
        pathicons = path.join(PATHSCRIPT, 'icons')
        self.icofolder = tk.PhotoImage(file=path.join(pathicons,'open_folder.png'))
        self.icoload = tk.PhotoImage(file=path.join(pathicons,'open_file.png'))
        self.icosave = tk.PhotoImage(file=path.join(pathicons,'save.png'))
        self.icoreset = tk.PhotoImage(file=path.join(pathicons,'reset.png'))
        self.icoclear = tk.PhotoImage(file=path.join(pathicons,'clear.png'))

        self
        
        self.helptext = tk.StringVar()
        self.statustext = tk.StringVar()

    def action_command(self, command):
        ''' The action to perform when the action button is pressed '''
        if self.runningtask:
            self.action_terminate()
        else:
            self.action_execute(command)

    def action_execute(self, command):
        ''' Execute the task in Faceswap.py '''
        self.clear_console()
        self.task.prepare(self.opts, command)
        self.task.execute_script()
    
    def action_terminate(self):
        ''' Terminate the subprocess Faceswap.py task '''
        self.task.terminate()
        self.runningtask = False
        self.change_action_button()

    def change_action_button(self):
        ''' Change the action button to relevant control '''
        for cmd in self.actiontext.keys():
            text = 'Terminate' if self.runningtask else cmd.title()
            self.actiontext[cmd].set(text)

    def bind_help(self, control, helptext):
        ''' Controls the help text displayed on mouse hover '''
        for action in ('<Enter>', '<FocusIn>', '<Leave>', '<FocusOut>'):
            helptext = helptext if action in ('<Enter>', '<FocusIn>') else ''
            control.bind(action, lambda event, txt=helptext: self.helptext.set(txt))

    def clear_console(self):
        ''' Clear the console output screen '''
        self.console.delete(1.0, tk.END)

    def load_config(self, command=None):
        ''' Load a saved config file '''
        cfgfile = filedialog.askopenfile(mode='r', filetypes=self.filetypes)
        if not cfgfile:
            return
        cfg = self.serializer.unmarshal(cfgfile.read())
        if command is None:
            for cmd, opts in cfg.items():
                self.set_command_args(cmd, opts)
        else:
            opts = cfg[command]
            self.set_command_args(command, opts)
                
    def set_command_args(self, command, options):
        ''' Pass the saved config items back to the GUI '''
        for srcopt, srcval in options.items():
            for dstopts in self.opts[command]:
                if dstopts['control_title'] == srcopt:
                    dstopts['value'].set(srcval)
                    break
        
    def save_config(self, command=None):
        ''' Save the current GUI state to a config file in json format '''
        cfgfile = filedialog.asksaveasfile( mode='w',
                                            filetypes=self.filetypes, 
                                            defaultextension='.fsw')
        if not cfgfile:
            return
        if command is None:
            cfg = {cmd: {opt['control_title']: opt['value'].get() for opt in opts} 
                   for cmd, opts in self.opts.items()}
        else:
            cfg = {command: {opt['control_title']: opt['value'].get()
                   for opt in self.opts[command]}}
        cfgfile.write(self.serializer.marshal(cfg))
        cfgfile.close

    def reset_config(self, command=None):
        ''' Reset the GUI to the default values '''
        if command is None:
            options = [opt for opts in self.opts.values() for opt in opts]
        else:
            options = [opt for opt in self.opts[command]]
        for option in options:
            default = option.get('default', '')
            default = '' if default is None else default
            option['value'].set(default)

    def clear_config(self, command=None):
        ''' Clear all values from the GUI '''
        if command is None:
            options = [opt for opts in self.opts.values() for opt in opts]
        else:
            options = [opt for opt in self.opts[command]]
        for option in options:
            if isinstance(option['value'].get(), bool):
                option['value'].set(False)
            elif isinstance(option['value'].get(), int):
                option['value'].set(0)
            else:
                option['value'].set('')

class FaceswapGui(object):
    ''' The Graphical User Interface '''
    def __init__(self, utils):
        self.gui = tk.Tk()
        self.utils = utils
        self.utils.init_tk()

    def build_gui(self):
        ''' Build the GUI '''
        self.gui.title('faceswap.py')
        self.menu()

        mainframe = ttk.Frame(self.gui)
        mainframe.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        optsnotebook = ttk.Notebook(mainframe)
        optsnotebook.pack(side=tk.LEFT, fill=tk.BOTH, expand=False)
    # Commands explicitly stated to ensure consistent ordering
        for command in ('extract', 'train', 'convert'):
            commandtab = CommandTab(self.utils, optsnotebook, command)
            commandtab.build_tab()

        dspnotebook = ttk.Notebook(mainframe)
        dspnotebook.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        for display in ('console', 'graph', 'preview'):
            displaytab = DisplayTab(self.utils, dspnotebook, display)
            displaytab.build_tab()
        self.add_status_bar()

    def menu(self):
        ''' Menu bar for loading and saving configs '''
        menubar = tk.Menu(self.gui)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label='Load full config...', command=self.utils.load_config)
        filemenu.add_command(label='Save full config...', command=self.utils.save_config)
        filemenu.add_separator()
        filemenu.add_command(label='Reset all to default', command=self.utils.reset_config)
        filemenu.add_command(label='Clear all', command=self.utils.clear_config)
        filemenu.add_separator()
        filemenu.add_command(label='Quit', command=self.gui.quit)
        menubar.add_cascade(label="File", menu=filemenu)
        self.gui.config(menu=menubar)

    def add_status_bar(self):
        ''' Build the info text section page '''
        statusframe = ttk.Frame(self.gui)
        statusframe.pack(side=tk.BOTTOM, anchor=tk.W, padx=10, pady=2, fill=tk.X, expand=False)
        
        lbltitle = ttk.Label(statusframe, text='Status:', width=6, anchor=tk.W)
        lbltitle.pack(side=tk.LEFT, expand=False)
        self.utils.statustext.set('Ready')
        lblstatus = ttk.Label(   statusframe,
                                width=20,
                                textvariable=self.utils.statustext,
                                anchor=tk.W)
        lblstatus.pack(side=tk.LEFT, anchor=tk.W, fill=tk.X, expand=True)

class CommandTab(object):
    ''' Tabs to hold the command options '''
    def __init__(self, utils, notebook, command):
        self.utils = utils
        self.notebook = notebook
        self.page = ttk.Frame(self.notebook)
        self.command = command
        self.title = command.title()

    def build_tab(self):
        ''' Build the tab '''
        actionframe = ActionFrame(self.utils, self.page, self.command)
        actionframe.build_frame()
        
        self.add_frame_seperator()
        opt_frame = self.add_right_frame()
        
        for option in self.utils.opts[self.command]:
            optioncontrol = OptionControl(self.utils, option, opt_frame)
            optioncontrol.build_full_control()
        self.notebook.add(self.page, text=self.title)

    def add_frame_seperator(self):
        ''' Add a seperator between left and right frames '''
        sep = ttk.Frame(self.page, width=2, relief=tk.SUNKEN)
        sep.pack(fill=tk.Y, padx=5, side=tk.LEFT)

    def add_right_frame(self):
        ''' Add the options panel to the right frame of each page '''
        frame = ttk.Frame(self.page)
        frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(0,5))

        canvas = tk.Canvas(frame, width=410, height=450, bd=0, highlightthickness=0)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.add_scrollbar(frame, canvas)

        optsframe = ttk.Frame(canvas)
        canvas.create_window((0,0), window=optsframe, anchor=tk.NW)

        return optsframe

    def add_scrollbar(self, frame, canvas):
        ''' Add a scrollbar to the options frame '''
        scrollbar = ttk.Scrollbar(frame, command=canvas.yview)
        scrollbar.pack(side=tk.LEFT, fill='y')
        canvas.configure(yscrollcommand = scrollbar.set)
        canvas.bind('<Configure>',lambda event, cvs=canvas: self.update_scrollbar(event, cvs))

    @staticmethod
    def update_scrollbar(event, canvas):
        canvas.configure(scrollregion=canvas.bbox('all'))

class ActionFrame(object):
    '''Action Frame - Displays information and action controls '''
    def __init__(self, utils, page, command):
        self.utils = utils
        self.page = page
        self.command = command
        self.title = command.title()

    def build_frame(self):
        ''' Add help display and Action buttons to the left frame of each page '''
        frame = ttk.Frame(self.page)
        frame.pack(fill=tk.X, padx=(10,5), side=tk.LEFT, anchor=tk.N)

        self.add_info_section(frame)
        self.add_action_button(frame)
        self.add_util_buttons(frame)
        
    def add_info_section(self, frame):
        ''' Build the info text section page '''
        hlpframe=ttk.Frame(frame)
        hlpframe.pack(fill=tk.X, side=tk.TOP, pady=5)
        lbltitle = ttk.Label(hlpframe, text='Info', width=15, anchor=tk.SW)
        lbltitle.pack(side=tk.TOP)
        self.utils.helptext.set('')
        lblhelp = tk.Label( hlpframe,
                            height=20,
                            width=15,
                            textvariable=self.utils.helptext,
                            wraplength=120, 
                            justify=tk.LEFT, 
                            anchor=tk.NW,
                            bg="gray90")
        lblhelp.pack(side=tk.TOP, anchor=tk.N)

    def add_action_button(self, frame):
        ''' Add the action buttons for page '''
        actvar = tk.StringVar(frame)
        actvar.set(self.title)
        self.utils.actiontext[self.command] = actvar
        
        actframe = ttk.Frame(frame)
        actframe.pack(fill=tk.X, side=tk.TOP, pady=(15, 0))

        btnact = tk.Button(  actframe,
                             textvariable=self.utils.actiontext[self.command],
                             height=2,
                             width=12,
                             command=lambda: self.utils.action_command(self.command))
        btnact.pack(side=tk.TOP)
        self.utils.bind_help(btnact, 'Run the {} script'.format(self.title))

    def add_util_buttons(self, frame):
        ''' Add the section utility buttons '''
        utlframe = ttk.Frame(frame)
        utlframe.pack(side=tk.TOP, pady=(5,0))

        for utl in ('load', 'save', 'clear', 'reset'):
            img = getattr(self.utils, 'ico' + utl)
            action = getattr(self.utils, utl + '_config')
            btnutl = ttk.Button( utlframe,
                                image=img,
                                command=lambda cmd=action: cmd(self.command))
            btnutl.pack(padx=2, pady=2, side=tk.LEFT)
            self.utils.bind_help(btnutl, utl.capitalize() + ' ' + self.title + ' config')

class OptionControl(object):
    ''' Build the correct control for the option parsed and place it on the frame '''
    def __init__(self, utils, option, option_frame):
        self.utils = utils
        self.option = option
        self.option_frame = option_frame
    
    def build_full_control(self):
        ''' Build the correct control type for the option passed through '''
        ctl = self.option['control']
        ctltitle = self.option['control_title']
        sysbrowser = self.option['filesystem_browser']
        ctlhelp = ' '.join(self.option.get('help', '').split())
        ctlhelp = '. '.join(i.capitalize() for i in ctlhelp.split('. '))
        ctlhelp = ctltitle + ' - ' + ctlhelp
        ctlframe = self.build_one_control_frame()
        dflt = self.option.get('default', '')
        dflt = self.option.get('default', False) if ctl == ttk.Checkbutton else dflt
        choices = self.option['choices'] if ctl == ttk.Combobox else None

        self.build_one_control_label(ctlframe, ctltitle)
        self.option['value'] = self.build_one_control(  ctlframe,
                                                        ctl,
                                                        dflt,
                                                        ctlhelp,
                                                        choices,
                                                        sysbrowser)

    def build_one_control_frame(self):
        ''' Build the frame to hold the control '''
        frame = ttk.Frame(self.option_frame)
        frame.pack(fill=tk.X)
        return frame
    
    def build_one_control_label(self, frame, control_title):
        ''' Build and place the control label '''
        lbl = ttk.Label(frame, text=control_title, width=15, anchor=tk.W)
        lbl.pack(padx=5, pady=5, side=tk.LEFT, anchor=tk.N)

    def build_one_control(self, frame, control, default, helptext, choices, sysbrowser):
        ''' Build and place the option controls '''
        default = default if default is not None else ''

        var = tk.BooleanVar(frame) if control == ttk.Checkbutton else tk.StringVar(frame)
        var.set(default)

        if sysbrowser is not None:
            self.add_browser_buttons(frame, sysbrowser, var)

        ctlkwargs = {'variable': var} if control == ttk.Checkbutton else {'textvariable': var}
        packkwargs = {'anchor': tk.W} if control == ttk.Checkbutton else {'fill': tk.X}

        if control == ttk.Combobox: #TODO: Remove this hacky fix to force the width of the frame
            ctlkwargs['width'] = 30

        ctl = control(frame, **ctlkwargs)
        
        if control == ttk.Combobox:
            ctl['values'] = [choice for choice in choices]
        
        ctl.pack(padx=5, pady=5, **packkwargs)

        self.utils.bind_help(ctl, helptext)
        return(var)

    def add_browser_buttons(self, frame, sysbrowser, filepath):
        ''' Add correct file browser button for control '''
        img = getattr(self.utils, 'ico' + sysbrowser)
        action = getattr(self, 'ask_' + sysbrowser)
        fileopn = ttk.Button(frame, image=img, command=lambda cmd=action: cmd(filepath))
        fileopn.pack(side=tk.RIGHT)

    @staticmethod
    def ask_folder(filepath):
        ''' Pop-up to get path to a folder '''
        dirname = filedialog.askdirectory()
        if dirname:
            filepath.set(dirname)
   
    @staticmethod
    def ask_load(filepath):
        ''' Pop-up to get path to a file '''
        filename = filedialog.askopenfilename()
        if filename:
            filepath.set(filename)
  
class DisplayTab(object):
    ''' The display tabs '''
    def __init__(self, utils, notebook, display):
        self.utils = utils
        self.notebook = notebook
        self.page = ttk.Frame(self.notebook)
        self.display = display
        self.title = self.display.title()
        
    def build_tab(self):
        ''' Build the tab '''
        frame = ttk.Frame(self.page)
        frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        if self.display == 'console':
            self.utils.console = tk.Text(frame, width=100, height=25, bg='gray90', fg='black')
            self.utils.console.pack(padx=5, pady=5, side=tk.LEFT, anchor=tk.N, fill=tk.BOTH, expand=True)

            scrollbar = ttk.Scrollbar(frame, command=self.utils.console.yview)
            scrollbar.pack(side=tk.LEFT, fill='y')
            self.utils.console.configure(yscrollcommand = scrollbar.set)
        
            sys.stdout = sys.stderr = ConsoleCapture(self.utils.console)
        elif self.display == 'graph':
            graph = Figure(figsize=(4,4), dpi=75)
            plt = graph.add_subplot(111)

            plt.plot([1,2,3,4,5,6,7,8],[5,6,1,3,8,9,3,5])
            plt.plot([1,2,3,4,5,6,7,8],[5,3,9,8,3,1,6,5])

            plt.legend(['Loss A', 'Loss B'], loc='lower left')

            canvas = FigureCanvasTkAgg(graph, frame)
            canvas.draw()
            canvas.get_tk_widget().pack(side=tk.BOTTOM, fill=tk.BOTH, expand=True)

            canvas._tkcanvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        else:
            lbl = ttk.Label(frame, text=self.display, width=15, anchor=tk.W)
            lbl.pack(padx=5, pady=5, side=tk.LEFT, anchor=tk.N)
        
        self.notebook.add(self.page, text=self.title)

class FaceswapControl(object):
    ''' Control the underlying Faceswap tasks '''
    def __init__(self, utils):
        self.pathfaceswap = path.join(PATHSCRIPT, 'faceswap.py')
        self.utils = utils
        
        self.command = None
        self.args = None
        self.process = None

    def prepare(self, options, command):
        ''' Prepare for running the subprocess '''
        self.command = command
        self.utils.runningtask = True
        self.utils.change_action_button()
        self.utils.statustext.set('Executing - ' + self.command + '.py')
        print('Loading...')
        self.args = ['python', '-u', self.pathfaceswap, self.command]
        self.build_args(options)

    def build_args(self, options):
        ''' Build the faceswap command and arguments list '''
        for item in options[self.command]:
            optval = str(item.get('value','').get())
            opt = item['opts'][0]
            if optval == 'False' or optval == '':
                continue
            elif optval == 'True':
                self.args.append(opt)
            else:
                self.args.extend((opt, optval))

    def execute_script(self):
        ''' Execute the requested Faceswap Script '''
        self.process = Popen(   self.args,
                                stdout=PIPE,
                                stderr=STDOUT,
                                bufsize=1,
                                universal_newlines=True)
        self.thread_stdout()

    def read_stdout(self):
        while True:
            output = self.process.stdout.readline()
            if output == '' and self.process.poll() is not None:
                break
            if output:
                print(output.strip())
        returncode = self.process.poll()
        self.utils.runningtask = False
        self.utils.change_action_button()
        self.set_final_status(returncode)

    def thread_stdout(self):
        thread = Thread(target=self.read_stdout)
        thread.start()

    def terminate(self):
        ''' Terminate the subprocess '''
        print('Terminating Process...')
        try:
            self.process.terminate()
            self.process.wait(timeout=10)
            print('Terminated')
        except TimeoutExpired:
            print('Termination timed out. Killing Process...')
            self.process.kill()
            print('Killed')

    def set_final_status(self, returncode):
        ''' Set the status bar output based on subprocess return code '''
        if returncode == 0:
            status = 'Ready'
        elif returncode == -15:
            status = 'Terminated - ' + self.command + '.py'
        elif returncode == -9:
            status = 'Killed - ' + self.command + '.py'
        else:
            status = 'Failed - ' + self.command + '.py'
        self.utils.statustext.set(status)


class ConsoleCapture(object):
    ''' Capture the console output and write to tkinter console window '''
    def __init__(self, console):
        self.console = console

    def write(self, string):
        ''' Capture stdout '''
        self.console.insert(tk.END, string)
        self.console.see(tk.END)

    @staticmethod
    def flush(): #TODO. Do something with this. Just here to suppress attribute error
        sys.stderr.flush()
        sys.stdout.flush()

class TKGui(object):
    ''' Main GUI Control '''
    def __init__ (self, subparser, subparsers, command, description='default'):
    # Don't try to load the GUI if there is no display or there are problems importing tkinter
        cmd = sys.argv
        if not check_display(cmd) or not import_tkinter(cmd):
            return
       
        self.opts = self.extract_options(subparsers)
        self.utils = Utils(self.opts)
        self.root = FaceswapGui(self.utils)
        self.parse_arguments(description, subparser, command)

    def extract_options(self, subparsers):
        ''' Extract the existing ArgParse Options '''
        opts = {cmd: subparsers[cmd].argument_list + 
                subparsers[cmd].optional_arguments for cmd in subparsers.keys()}
        for command in opts.values():
            for opt in command:
                ctl, sysbrowser = self.set_control(opt)
                opt['control_title'] = self.set_control_title(opt.get('opts',''))
                opt['control'] = ctl
                opt['filesystem_browser'] = sysbrowser
        return opts

    @staticmethod
    def set_control_title(opts):
        ''' Take the option switch and format it nicely '''
        ctltitle = opts[1] if len(opts) == 2 else opts[0]
        ctltitle = ctltitle.replace('-',' ').replace('_',' ').strip().title()
        return ctltitle
 
    @staticmethod
    def set_control(option):
        ''' Set the control and filesystem browser to use for each option '''
        sysbrowser = None
        ctl = ttk.Entry
        if option.get('dest', '') == 'alignments_path':
            sysbrowser = 'load'
        elif option.get('action', '') == FullPaths:
            sysbrowser = 'folder'
        elif option.get('choices', '') != '':
            ctl = ttk.Combobox
        elif option.get('action', '') == 'store_true':
            ctl = ttk.Checkbutton
        return ctl, sysbrowser

    def parse_arguments(self, description, subparser, command):
        parser = subparser.add_parser(
            command,
            help="This Launches a GUI for Faceswap.",
            description=description,
            epilog="Questions and feedback: \
            https://github.com/deepfakes/faceswap-playground"
        )
        parser.set_defaults(func=self.process)        

    def process(self, arguments):
        ''' Builds the GUI '''
        self.arguments = arguments
        self.root.build_gui()
        self.root.gui.mainloop()

