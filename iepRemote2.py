#   Copyright (c) 2010, Almar Klein
#   All rights reserved.
#
#   This file is part of IEP.
#    
#   IEP is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
# 
#   IEP is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
# 
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.

""" Module iepRemote2
Implements the IEP interpreter and the introspection thread.
Also GUI hijacking is defined here. This code works on all* python versions.
*: Well, at least from 2.4 and up (including py3k).

Note that this module delibirately has a name that is very unlikely to 
occur in any othe packages to prevent import clashes.
"""

import os, sys, time
from codeop import CommandCompiler
import traceback
import types
import threading
import inspect
import keyword # for autocomp

# Init last traceback information
sys.last_type = None
sys.last_value = None
sys.last_traceback = None


class IepInterpreter:
    """ Closely emulate the interactive Python console.
    Simular working as code.InteractiveConsole. Some code was copied, but
    the following things are changed:
    - prompts are printed in the err stream, like the default interpreter does
    - uses an asynchronous read using the channels interface
    - support for hijacking GUI toolkits
    - can run large pieces of code
    - support post mortem debugging
    """
    
    def __init__(self, locals, filename="<console>", 
                    gui='', runsus=True, startdir=''):
        
        # Init variables for locals and globals (globals only for debugging)
        self.locals = locals
        self.globals = None
        
        # Store other variables
        self.filename = filename
        self._runsus = runsus
        self._startdir = startdir
        self._gui = gui
        
        # Store ref of locals that is our main
        self._main_locals = locals
        
        # Information for debugging. If self._dbFrames, we're in debug mode
        # _dbFrameIndex starts from 1 
        self._dbFrames = []
        self._dbFrameIndex = 0
        self._dbFrameName = ''
        
        # Init datase to store source code that we execute
        self._codeCollection = ExecutedSourceCollection()
        
        # Init buffer to deal with multi-line command in the shell
        self.buffer = []
        
        # Init the compiler
        self.compile = CommandCompiler()
        
        # Define prompts
        try:
            sys.ps1
        except AttributeError:
            sys.ps1 = ">>> "
        try:
            sys.ps2
        except AttributeError:
            sys.ps2 = "... "
    
    
    ## Base of interpreter
    
    def interact(self):    
        """ Interact! (start the mainloop)
        """
        
        # Hijack GUI toolkit
        self.guiApp = None
        try:
            if self._gui == 'tk':
                self.guiApp = Hijacked_tk()
            elif self._gui == 'wx':
                self.guiApp = Hijacked_wx()
            elif self._gui == 'qt4':
                self.guiApp = Hijacked_qt4()
            elif self._gui == 'fl':
                self.guiApp = Hijacked_fltk()
        except ImportError:
            pass
        
        # Create banner
        cprt =  'Type "help", "copyright", "credits" or "license"'\
                ' for more information.'
        if self.guiApp:
            tmp = self._gui.upper()
            moreBanner = 'This is the IEP interpreter (with %s). ' % (tmp) 
            moreBanner += 'Type "?" for a list of *magic* commands.'
        else:
            moreBanner = 'This is the IEP interpreter. ' 
            moreBanner += 'Type "?" for a list of *magic* commands.'
        sys.stdout.write("Python %s on %s.\n%s\n%s\n" %
            (sys.version, sys.platform, cprt, moreBanner))
        
        # Remove "THIS" directory from the PYTHONPATH
        # to prevent unwanted imports
        thisPath = os.getcwd()
        if thisPath in sys.path:
            sys.path.remove(thisPath)
        
        # Go to start dir
        if self._startdir and os.path.isdir(self._startdir):
            os.chdir(self._startdir)
        else:
            os.chdir(os.path.expanduser('~')) # home dir        
            #os.chdir(sys.exec_prefix) On windows, python starts here
        
        # Execute startup script
        filename = os.environ.get('PYTHONSTARTUP')
        if self._runsus and filename and os.path.isfile(filename):
            exec(open(filename).read(), self.locals)
            #execfile(filename, self.locals) # removed in py3k
        
        # ENTER MAIN LOOP
        guitime = time.time()
        more = 0
        self.newPrompt = True
        while True:
            try:
                
                # Set status and prompt?
                # Prompt is allowed to be an object with __str__ method
                if self.newPrompt:
                    self.newPrompt = False
                    # Write prompt
                    preamble = ''
                    if self._dbFrames:
                        preamble = '('+self._dbFrameName+')'
                    if more:
                        self.write(preamble+str(sys.ps2))
                    else:
                        self.write(preamble+str(sys.ps1))
                    # Set status
                    self.writeStatus()
                
                # Wait for a bit at each round
                time.sleep(0.010) # 10 ms
                
                # Read control stream and process
                control = sys._control.readOne(False)
                if control:
                    self.parsecontrol(control)
                
                # Are we still connected?
                if sys.stdin.closed:
                    self.write("\n")
                    break
                
                # Read a packet and process
                line = sys.stdin.readOne(False)
                if line:
                    # Set busy
                    sys._status.write('Busy')
                    self.newPrompt = True
                    
                    if line.startswith('\n') and len(line)>1:
                        # Execute larger piece of code
                        self.runlargecode(line)
                        # Reset more stuff
                        self.resetbuffer()
                        more = False
                    else:
                        # Execute line
                        line = line.rstrip("\n") # this is what push wants
                        more = self.push(line)
                
                # Keep GUI toolkit up to date
                if self.guiApp and time.time() - guitime > 0.019:
                    self.guiApp.processEvents()
                    guitime = time.time()
            
            except KeyboardInterrupt:
                self.write("\nKeyboardInterrupt\n")
                self.resetbuffer()
                more = 0
    
    
    def resetbuffer(self):
        """Reset the input buffer."""
        self.buffer = []
    
    
    def push(self, line):
        """Push a line to the interpreter.
        
        The line should not have a trailing newline; it may have
        internal newlines.  The line is appended to a buffer and the
        interpreter's runsource() method is called with the
        concatenated contents of the buffer as source.  If this
        indicates that the command was executed or invalid, the buffer
        is reset; otherwise, the command is incomplete, and the buffer
        is left as it was after the line was appended.  The return
        value is 1 if more input is required, 0 if the line was dealt
        with in some way (this is the same as runsource()).
        
        """
        self.buffer.append(line)
        source = "\n".join(self.buffer)
        more = self.runsource(source, self.filename)
        if not more:
            self.resetbuffer()
        return more
    
    
    def runsource(self, source, filename="<input>", symbol="single"):
        """Compile and run some source in the interpreter.
        
        Arguments are as for compile_command().
        
        One several things can happen:
        
        1) The input is incorrect; compile_command() raised an
        exception (SyntaxError or OverflowError).  A syntax traceback
        will be printed by calling the showsyntaxerror() method.
        
        2) The input is incomplete, and more input is required;
        compile_command() returned None.  Nothing happens.
        
        3) The input is complete; compile_command() returned a code
        object.  The code is executed by calling self.runcode() (which
        also handles run-time exceptions, except for SystemExit).
        
        The return value is True in case 2, False in the other cases (unless
        an exception is raised).  The return value can be used to
        decide whether to use sys.ps1 or sys.ps2 to prompt the next
        line.
        
        """
        try:
            code = self.compile(source, filename, symbol)
        except (OverflowError, SyntaxError, ValueError):
            # Case 1
            self.showsyntaxerror(filename)
            return False
        
        if code is None:
            # Case 2
            return True
        
        # Case 3
        self.runcode(code)
        return False
    
    
    def runcode(self, code):
        """Execute a code object.

        When an exception occurs, self.showtraceback() is called to
        display a traceback.  All exceptions are caught except
        SystemExit, which is reraised.

        A note about KeyboardInterrupt: this exception may occur
        elsewhere in this code, and may not always be caught.  The
        caller should be prepared to deal with it.
        
        The globals variable is used when in debug mode.
        """
        try:
            if self._dbFrames:
                exec(code, self.globals, self.locals)
            else:
                exec(code, self.locals)
        except SystemExit:
            raise
        except KeyboardInterrupt:
            raise
        except Exception:
            self.showtraceback()
    
    
    def runlargecode(self, text):
        """ To execute larger pieces of code. """
        
        # Split information
        # (The last line contains filename + lineOffset about the code)
        tmp = text.rsplit('\n', 2)
        source = tmp[0][1:]  # remove first newline
        fname = tmp[1]
        lineno = int(tmp[2])
        
        # Put the line number in the filename (if necessary)
        # Note that we could store the line offset in the _codeCollection,
        # but then we cannot retrieve it for syntax errors.
        if lineno:
            fname = "%s+%i" % (fname, lineno)
        
        # Try compiling the source
        code = None
        try:            
            # Compile
            code = self.compile(source, fname, "exec")          
            
        except (OverflowError, SyntaxError, ValueError):
            self.showsyntaxerror(fname)
        
        if code:
            # Store the source using the (id of the) code object as a key
            self._codeCollection.storeSource(code, source)
            # Execute the code
            self.runcode(code)
    
    
    ## Misc
    
    def parsecontrol(self, control):
        """ Parse a command received on the control stream. 
        This is used to request the status and to control the
        (post mortem) debugging.
        """
        
        if control == 'STATUS':
            self.writeStatus()
        
        elif control == 'DEBUG START':
            # Collect frames from the traceback
            tb = sys.last_traceback
            frames = []
            while tb:
                frames.append(tb.tb_frame)
                tb = tb.tb_next
            # Enter debug mode if there was an error
            if frames:
                self._dbFrames = frames
                self._dbFrameIndex = len(self._dbFrames)
                frame = self._dbFrames[self._dbFrameIndex-1]
                self._dbFrameName = frame.f_code.co_name
                self.locals = frame.f_locals
                self.globals = frame.f_globals
                # Notify IEP
                self.writeStatus()
            else:
                self.write("No debug information available.\n")
        
        elif control.startswith('DEBUG') and not self._dbFrames:
            # Ignoire other debug commands when not debugging
            self.write("Not in debug mode.\n")
        
        elif control.startswith('DEBUG INDEX'):
            # Set frame index
            self._dbFrameIndex = int(control.rsplit(' ',1)[-1])
            if self._dbFrameIndex < 1:
                self._dbFrameIndex = 1
            elif self._dbFrameIndex > len(self._dbFrames):
                self._dbFrameIndex = len(self._dbFrames)
            # Set name and locals
            frame = self._dbFrames[self._dbFrameIndex-1]
            self._dbFrameName = frame.f_code.co_name
            self.locals = frame.f_locals
            self.globals = frame.f_globals
        
        elif control == 'DEBUG UP':
            # Decrease frame index
            self._dbFrameIndex -= 1
            if self._dbFrameIndex < 1:
                self._dbFrameIndex = 1
            # Set name and locals
            frame = self._dbFrames[self._dbFrameIndex-1]
            self._dbFrameName = frame.f_code.co_name
            self.locals = frame.f_locals
            self.globals = frame.f_globals
        
        elif control == 'DEBUG DOWN':
            # Increase frame index
            self._dbFrameIndex += 1
            if self._dbFrameIndex > len(self._dbFrames):
                self._dbFrameIndex = len(self._dbFrames)
            # Set name and locals
            frame = self._dbFrames[self._dbFrameIndex-1]
            self._dbFrameName = frame.f_code.co_name
            self.locals = frame.f_locals
            self.globals = frame.f_globals
        
        elif control == 'DEBUG STOP':
            self.locals = self._main_locals
            self.globals = None
            self._dbFrames = []
    
    
    ## Writing and error handling
    
    
    def write(self, text):
        """ Write errors and prompts. """
        sys.stderr.write( text )
    
    
    def writeStatus(self):
        """ Write the status (Ready, or Busy, or Debug info). """
        if self._dbFrames:
            # Debug info
            stack = [str(self._dbFrameIndex)]
            for f in self._dbFrames:
                # Get fname and lineno, and correct if required
                fname, lineno = f.f_code.co_filename, f.f_lineno
                fname, lineno = correctFilenameAndLineno(fname, lineno)
                # Build string
                text = f.f_code.co_name + ': '
                text += 'line ' + str(lineno) + ' in ' + fname
                stack.append(text)
            sys._status.write('Debug ' + ','.join(stack))
        else:
            sys._status.write('Ready')
    
    
    def showsyntaxerror(self, filename=None):
        """Display the syntax error that just occurred.
        This doesn't display a stack trace because there isn't one.        
        If a filename is given, it is stuffed in the exception instead
        of what was there before (because Python's parser always uses
        "<string>" when reading from a string).
        
        IEP version: support to display the right line number,
        see doc of showtraceback for details.        
        """
        
        # Get info (do not store)
        type, value, tb = sys.exc_info()
        tb = None
        
        # Work hard to stuff the correct filename in the exception
        if filename and type is SyntaxError:
            try:
                # unpack information
                msg, (dummy_filename, lineno, offset, line) = value
                # correct line-number
                fname, lineno = correctFilenameAndLineno(filename, lineno)
            except:
                # Not the format we expect; leave it alone
                pass
            else:
                # Stuff in the right filename
                value = SyntaxError(msg, (fname, lineno, offset, line))
                sys.last_value = value
        
        # Show syntax error 
        strList = traceback.format_exception_only(type, value)
        for s in strList:
            self.write(s)
    
    
    def showtraceback(self):
        """Display the exception that just occurred.
        We remove the first stack item because it is our own code.
        The output is written by self.write(), below.
        
        In the IEP version, before executing a block of code,
        the filename is modified by appending " [x]". Where x is
        the index in a list that we keep, of tuples 
        (sourcecode, filename, lineno). 
        
        Here, showing the traceback, we check if we see such [x], 
        and if so, we extract the line of code where it went wrong,
        and correct the lineno, so it will point at the right line
        in the editor if part of a file was executed. When the file
        was modified since the part in question was executed, the
        fileno might deviate, but the line of code shown shall 
        always be correct...
        """
        # Traceback info:
        # tb_next -> go down the trace
        # tb_frame -> get the stack frame
        # tb_lineno -> where it went wrong
        #
        # Frame info:
        # f_back -> go up (towards caller)
        # f_code -> code object
        # f_locals -> we can execute code here when PM debugging
        # f_globals
        # f_trace -> (can be None) function for debugging? (
        #
        # The traceback module is used to obtain prints from the
        # traceback.
        
        try:
            # Get exception information and remove first, since that's us
            type, value, tb = sys.exc_info()
            tb = tb.tb_next
            
            # Store for debugging, but only store if not in debug mode
            if not self._dbFrames:
                sys.last_type = type
                sys.last_value = value
                sys.last_traceback = tb
            
            # Get frame
            frame = tb.tb_frame
            
            # Get source (if available) and split lines
            source = self._codeCollection.getSource(frame.f_code)
            source = source.splitlines()
            
            # Get tpraceback to correct all the line numbers
            # tblist = list  of (filename, line-number, function-name, text)
            tblist = traceback.extract_tb(tb)
            
            # Walk through the list
            for i in range(len(tblist)):
                tb = tblist[i]
                # Get filename and line number
                fname, lineno = correctFilenameAndLineno(tb[0], tb[1])
                # Obtain source from example and select line
                example = tb[3]
                if source:
                    try:
                        example = source[ tb[1]-1 ]
                    except IndexError:
                        pass
                # Reset info
                tblist[i] = (fname, lineno, tb[2], example)
            
            # Format list
            strList = traceback.format_list(tblist)
            if strList:
                strList.insert(0, "Traceback (most recent call last):\n")
            strList.extend( traceback.format_exception_only(type, value) )
            
            # Write traceback
            for s in strList:
                self.write(s)
            
        finally:
            tblist = tb = None
        

def correctFilenameAndLineno(fname, lineno):
    """ Given a filename and lineno, this function returns
    a modified (if necessary) version of the two. 
    As example:
    "foo.py+7", 22  -> "foo.py", 29
    """
    j = fname.find('+')
    if j>0:
        try:
            lineno += int(fname[j+1:])
            fname = fname[:j]
        except ValueError:
            pass
    return fname, lineno


class ExecutedSourceCollection(dict):
    """ Stores the source of executed pieces of code, so that the right 
    traceback can be reproduced when an error occurs.
    The codeObject produced by compiling the source is used as a 
    reference.
    """
    def _getId(self, codeObject):
        id_ = str(id(codeObject)) + '_' + codeObject.co_filename
    def storeSource(self, codeObject, source):
        self[self._getId(codeObject)] = source
    def getSource(self, codeObject):
        return self.get(self._getId(codeObject), '')


class IntroSpectionThread(threading.Thread):
    """ IntroSpectionThread
    Communicates with the IEP GUI, even if the main thread is busy.
    """
    
    def __init__(self, requestChannel, responseChannel, interpreter):
        threading.Thread.__init__(self)
        
        # store the two channel objects
        self.request = requestChannel
        self.response = responseChannel
        self.interpreter = interpreter
    
    
    def run(self):
        """ This is the "mainloop" of our introspection thread.
        """ 
        
        while True:
            
            # sleep for a bit
            time.sleep(0.01)
            
            # read code (wait here)
            line = self.request.readOne(True)
            if not line or self.request.closed:
                break # from thread
            
            # get request and arg
            tmp = line.split(" ",1)
            try:
                req = tmp[0]
                arg = tmp[1]
            except Exception:
                self.response.write('<not a valid request>')
                continue
            
            # process request
            
            if req == "EVAL":
                self.enq_eval( arg )
            
            elif req == "SIGNATURE":
                self.enq_signature(arg)
                
            elif req == "ATTRIBUTES":
                self.enq_attributes(arg)
            
            elif req == "HELP":
                self.enq_help(arg)
            
            else:
                self.response.write('<not a valid request>')
                
        print('IntrospectionThread stopped')
    
    
    def getNameSpace(self):
        """ Get the namespace to apply introspection in. 
        This is necessary in order to be able to use inspect
        in calling eval.
        """
        NS1 = self.interpreter.locals
        NS2 = self.interpreter.globals
        if not NS2:
            return NS1
        else:
            NS3 = {}
            for key in NS1:
                NS3[key] = NS1[key]
            for key in NS2:
                NS3[key] = NS2[key]
            return NS3
    
    
    def getSignature(self,objectName):
        """ Get the signature of builtin, function or method.
        Returns a tuple (signature_string, kind), where kind is a string
        of one of the above. When none of the above, both elements in
        the tuple are an empty string.
        """
        
        # if a class, get init
        # not if an instance! -> try __call__ instead        
        # what about self?
        
        # find out what kind of function, or if a function at all!
        NS = self.getNameSpace()
        fun1 = eval("inspect.isbuiltin(%s)"%(objectName), None, NS)
        fun2 = eval("inspect.isfunction(%s)"%(objectName), None, NS)
        fun3 = eval("inspect.ismethod(%s)"%(objectName), None, NS)
        fun4 = False
        fun5 = False
        if not (fun1 or fun2 or fun3):
            # Maybe it's a class with an init?
            if eval("isinstance(%s,type)"%(objectName), None, NS):
                if eval("hasattr(%s,'__init__')"%(objectName), None, NS):
                    objectName += ".__init__"
                    fun4 = eval("inspect.ismethod(%s)"%(objectName), None, NS)
            #  Or a callable object?
            elif eval("hasattr(%s,'__call__')"%(objectName), None, NS):
                objectName += ".__call__"
                fun5 = eval("inspect.ismethod(%s)"%(objectName), None, NS)
                
        if fun1:
            # the first line in the docstring is usually the signature
            kind = 'builtin'
            tmp = eval("%s.__doc__"%(objectName), {}, NS )
            sigs = tmp.splitlines()[0]
            if not ( sigs.count("(") and sigs.count(")") ):
                sigs = ""
                kind = ''            
            
        elif fun2 or fun3 or fun4 or fun5:
            
            if fun2:
                kind = 'function'
            elif fun3:
                kind = 'method'
            elif fun4:
                kind = 'class'
            elif fun5:
                kind = 'callable'
            
            # collect
            tmp = eval("inspect.getargspec(%s)"%(objectName), None, NS)
            args, varargs, varkw, defaults = tmp
            
            # prepare defaults
            if defaults == None:
                defaults = ()
            defaults = list(defaults)
            defaults.reverse()
            # make list (back to forth)
            args2 = []
            for i in range(len(args)-fun4):
                arg = args.pop()
                if i < len(defaults):
                    args2.insert(0, "%s=%s" % (arg, defaults[i]) )
                else:
                    args2.insert(0, arg )
            # append varargs and kwargs
            if varargs:
                args2.append( "*"+varargs )
            if varkw:
                args2.append( "**"+varkw )
            
            # append the lot to our  string
            funname = objectName.split('.')[-1]
            sigs = "%s(%s)" % ( funname, ", ".join(args2) )
            
        else:
            sigs = ""
            kind = ""
        
        return sigs, kind
    
    
    def enq_signature(self, objectName):
        
        try:
            text, kind = self.getSignature(objectName)
        except Exception:
            text = None
            
        # respond
        if text:
            self.response.write( text)
        else:
            self.response.write( "<error>" )
    
    
    def enq_attributes(self, objectName):
        
        # Get namespace
        NS = self.getNameSpace()
        
        # Init names
        names = set()
        
        # Obtain all attributes of the class
        try:
            command = "dir(%s.__class__)" % (objectName)
            d = eval(command, {}, NS)
        except Exception:            
            pass
        else:
            names.update(d)
        
        # Obtain instance attributes
        try:
            command = "%s.__dict__.keys()" % (objectName)
            d = eval(command, {}, NS)
        except Exception:            
            pass
        else:
            names.update(d)
            
        # That should be enough, but in case __dir__ is overloaded,
        # query that as well
        try:
            command = "dir(%s)" % (objectName)
            d = eval(command, {}, NS)
        except Exception:            
            pass
        else:
            names.update(d)
        
        # Respond
        if names:
            self.response.write( ",".join(list(names)) )
        else:
            self.response.write( "<error>" )
    
    
    def enq_help(self,objectName):
        """ get help on an object """
        
        # Get namespace
        NS = self.getNameSpace()
        
        try:
            # collect docstring
            h_text = ''
            # Try using the class (for properties)
            try:
                className = eval("%s.__class__.__name__"%(objectName), {}, NS)
                tmp = objectName.rsplit('.',1)
                if len(tmp)==2 and className not in ['type', 'module']:
                    cmd = "%s.__class__.%s.__doc__"
                    h_text = eval(cmd % (tmp[0],tmp[1]), {}, NS)
            except Exception:
                pass
            # Normal doc
            if not h_text:
                h_text = eval("%s.__doc__"%(objectName), {}, NS )
            
            # collect more data            
            h_repr = eval("repr(%s)"%(objectName), {}, NS )
            try:
                h_class = eval("%s.__class__.__name__"%(objectName), {}, NS )
            except Exception:
                h_class = "unknown"
            
            # docstring can be None, but should be empty then
            if not h_text:
                h_text = ""
            
            # get and correct signature
            h_fun, kind = self.getSignature(objectName)
            if kind == 'builtin' or not h_fun:
                h_fun = ""  # signature already in docstring or not available
            
            # cut repr if too long
            if len(h_repr) > 200:
                h_repr = h_repr[:200] + "..."                
            # replace newlines so we can separates the different parts
            h_repr = h_repr.replace('\n', '\r')
            
            # build final text
            text = '\n'.join([objectName, h_class, h_fun, h_repr, h_text])
        
        except Exception:
            text = "No help available."
        
        # The lines below can be uncomented for debugging, but they don't
        # work on python < 2.6.
#         except Exception as why:            
#            text = "No help available." + str(why)
        
        # Done
        self.response.write( text )
    
    
    def enq_eval(self, command):
        """ do a command and send "str(result)" back. """
        
        # Get namespace
        NS = self.getNameSpace()
        
        try:
            # here globals is None, so we can look into sys, time, etc...
            d = eval(command, None, NS)
        except Exception:            
            d = None
        
        # respond
        if d:
            self.response.write( str(d) )
        else:
            self.response.write( '<error>' )
    

## GUI TOOLKIT HIJACKS


class Hijacked_tk:    
    """ Tries to import Tkinter and returns a withdrawn Tkinter root
    window.  If Tkinter is already imported or not available, this
    returns None.  
    Modifies Tkinter's mainloop with a dummy so when a module calls
    mainloop, it does not block.
    """    
    def __init__(self):
        
        # Try importing        
        import Tkinter
        
        # Replace mainloop
        def dummy_mainloop(*args,**kwargs):
            pass
        Tkinter.Misc.mainloop = dummy_mainloop
        Tkinter.mainloop = dummy_mainloop
        
        # Create tk app and withdraw
        r = Tkinter.Tk()
        r.withdraw()
        
        # Store the app instance to process events
        self.app = r
    
    def processEvents(self):
        self.app.update()


class Hijacked_fltk:
    """ Hijack fltk 1.
    This one is easy. Just call fl.wait(0.0) now and then.
    Note that both tk and fltk try to bind to PyOS_InputHook. Fltk
    will warn about not being able to and Tk does not, so we should
    just hijack (import) fltk first. The hook that they try to fetch
    is not required in IEP, because the IEP interpreter will keep
    all GUI backends updated when idle.
    """
    def __init__(self):
        # Try importing        
        import fltk as fl
        
        # Replace mainloop with a dummy
        def dummyrun(*args,**kwargs):
            pass
        fl.Fl.run = types.MethodType(dummyrun, fl.Fl)
        
        # Store the app instance to process events
        self.app =  fl.Fl
    
    def processEvents(self):
        self.app.wait(0)


class Hijacked_fltk2:
    """ Hijack fltk 2.    
    """
    def __init__(self):
        # Try importing
        import fltk2 as fl        
        
        # Replace mainloop with a dummy
        def dummyrun(*args,**kwargs):
            pass    
        fl.run = dummyrun    
        
        # Return the app instance to process events
        self.app = fl
    
    def processEvents(self):
        # is this right?
        self.app.wait(0) 


class Hijacked_qt4:
    """ Hijack the pyqt4 mainloop.
    """
    
    def __init__(self):
        # Try importing qt        
        from PyQt4 import QtGui, QtCore
        
        # Create app class
        class QHijackedApp(QtGui.QApplication):
            def __init__(self):
                QtGui.QApplication.__init__(self,[])
            def __call__(self, *args, **kwargs):
                return QtGui.qApp
            def exec_(self, *args, **kwargs):
                pass
        
        # Store the app instance to process events 
        QtGui.QApplication = QtGui.qApp = app = QHijackedApp()
        self.app = app
    
    def processEvents(self):
        self.app.flush()
        self.app.processEvents()


class Hijacked_wx:
    """ Hijack the wxWidgets mainloop.    
    """ 
    
    def __init__(self):
        
        # Try importing
        try:
            import wx
        except ImportError:            
            # For very old versions of WX
            import wxPython as wx
        
        # Create dummy mainloop to replace original mainloop
        def dummy_mainloop(*args, **kw):
            pass
        
        # Depending on version, replace mainloop
        ver = wx.__version__
        orig_mainloop = None
        if ver[:3] >= '2.5':
            if hasattr(wx, '_core_'): core = getattr(wx, '_core_')
            elif hasattr(wx, '_core'): core = getattr(wx, '_core')
            else: raise ImportError
            orig_mainloop = core.PyApp_MainLoop
            core.PyApp_MainLoop = dummy_mainloop
        elif ver[:3] == '2.4':
            orig_mainloop = wx.wxc.wxPyApp_MainLoop
            wx.wxc.wxPyApp_MainLoop = dummy_mainloop
        else:
            # Unable to find either wxPython version 2.4 or >= 2.5."
            raise ImportError
        
        # Store the app instance to process events    
        self.wx = wx
        self.app = wx.PySimpleApp()
        #self.app = wx.App(redirect=False)
        #self.app.SetExitOnFrameDelete(False)
        #self.app.RestoreStdio()
    
    def processEvents(self):
        wx = self.wx
        
        # This bit is really needed        
        old = wx.EventLoop.GetActive()                       
        eventLoop = wx.EventLoop()
        wx.EventLoop.SetActive(eventLoop)                        
        while eventLoop.Pending():
            eventLoop.Dispatch()
        
        # Process and reset
        self.app.ProcessIdle() # otherwise frames do not close
        wx.EventLoop.SetActive(old)   