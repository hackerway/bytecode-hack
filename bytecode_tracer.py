import opcode
import re

from types import CodeType

from py_frame_object import get_value_stack


class ValueStack(object):
    """CPython stack that holds values used and generated during computation.

    Right before a function call value stack looks like this:

        +---------------------  <--- frame.f_valuestack
        | function object
        +----------
        | ...
        | list of positional arguments
        | ...
        +----------
        | ...
        | flat list of keyword arguments (key-value pairs)
        | ...
        +----------
        | *varargs tuple
        +----------
        | **kwargs dictionary
        +---------------------  <--- frame.f_stacktop

    When a function is called with no arguments, the function object is at the
    top of the stack. When arguments are present, they are placed above the
    function object. Two bytes after the CALL_FUNCTION bytecode contain number
    of positional and keyword arguments passed (see `unpack_CALL_FUNCTION` for
    details). Bytecode number tells us whether a call included single star
    (*args) and/or double star (**kwargs) arguments.

    Normally, to get to the interesting values at the stack, we would look at it
    from the top. Unfortunatelly, CPython clears f_stacktop when the frame is
    activated. This means we have to look at the stack from the bottom and
    figure out how many objects to skip. For example, before the function call,
    if we want to read the arguments, we only have to skip the function
    object. After a function return, the return value is the only element on
    stack, so there's no need to skip anything.

    Situation changes when a 'finally' block is entered - the bottom of the
    stack contains None, and we have to remember to skip it both before function
    calls and returns that happen inside that block.

    Similar thing happens during a 'for' loop. Bottom of the stack contains
    an iterator object. This heuristic of getting to the top of the stack is
    hopefully correct, but there always may be some corner cases I didn't
    think of.
    """
    def __init__(self, frame):
        self.stack = get_value_stack(frame)

        try:
            bcode, self.positional_args_count, self.keyword_args_count = unpack_CALL_FUNCTION(frame)
            self.args_count = self.positional_args_count + 2*self.keyword_args_count
            # There are four bytecodes for function calls, that tell use whether
            # single star (*args) and/or double star (**kwargs) notation was
            # used: CALL_FUNCTION, CALL_FUNCTION_VAR, CALL_FUNCTION_KW
            # and CALL_FUNCTION_VAR_KW.
            self.singlestar = "_VAR" in bcode
            self.doublestar = "_KW" in bcode
        except ValueError:
            pass

        self.offset = 0
        if not callable(self.stack[0]):
            self.offset = 1

        self.args_start = self.offset + 1

    def bottom(self):
        """The first object at the value stack.

        It's the function being called for CALL_FUNCTION_* bytecodes and a return
        value right after the function returns.
        """
        return self.stack[self.offset]

    def positional_args(self):
        """List of all positional arguments passed to a C function.
        """
        args = self.positional_args_from_stack()[:]
        if self.singlestar:
            args.extend(self.positional_args_from_varargs())
        return args

    def positional_args_from_stack(self):
        """Objects explicitly placed on stack as positional arguments.
        """
        positional_start = self.args_start
        return self.stack[positional_start:positional_start+self.positional_args_count]

    def positional_args_from_varargs(self):
        """Iterable placed on stack as "*args".
        """
        return self.stack_above_args()

    def keyword_args(self):
        """Dictionary of all keyword arguments passed to a C function.
        """
        kwds = self.keyword_args_from_stack().copy()
        if self.doublestar:
            kwds.update(self.keyword_args_from_double_star())
        return kwds

    def keyword_args_from_stack(self):
        """Key/value pairs placed explicitly on stack as keyword arguments.
        """
        keywords_start = self.args_start + self.positional_args_count
        args = self.stack[keywords_start:keywords_start+2*self.keyword_args_count]
        return flatlist_to_dict(args)

    def keyword_args_from_double_star(self):
        """Dictionary passed as "**kwds".
        """
        if self.singlestar:
            return self.stack_above_args(offset=1)
        else:
            return self.stack_above_args()

    def stack_above_args(self, offset=0):
        """For functions with *varargs and **kwargs will contain a tuple and/or
        a dictionary. It is an error to access it for other function calls.
        """
        i = self.args_start + self.args_count + offset
        return self.stack[i]


def flatlist_to_dict(alist):
    return dict(zip(alist[::2], alist[1::2]))

def unpack_CALL_FUNCTION(frame):
    """Number of arguments placed on stack is encoded as two bytes after
    the CALL_FUNCTION bytecode.
    """
    code = frame.f_code.co_code[frame.f_lasti:]
    name = opcode.opname[ord(code[0])]
    if name.startswith("CALL_FUNCTION"):
        return name, ord(code[1]), ord(code[2])

def current_bytecode(frame):
    code = frame.f_code.co_code[frame.f_lasti]
    return opcode.opname[ord(code)]

def is_c_func(func):
    """Return True if given function object was implemented in C,
    via a C extension or as a builtin.

    >>> is_c_func(repr)
    True
    >>> import sys
    >>> is_c_func(sys.exit)
    True
    >>> import doctest
    >>> is_c_func(doctest.testmod)
    False
    """
    return not hasattr(func, 'func_code')

class BytecodeTracer(object):
    def __init__(self):
        self.call_stack = []

    def trace(self, frame, event):
        """Tries to recognize the current event in terms of calls to and returns
        from C.

        Currently supported events:
         * ('c_call', (function, positional_arguments, keyword_arguments))
           A call to a C function with given arguments is about to happen.
         * ('c_return', return_value)
           A C function returned with given value (it will always be the function
           for the most recent 'c_call' event.
         * ('print', None)
           A print statement is about to be executed.

        In other cases, None is returned.
        """
        if event == 'line':
            bcode = current_bytecode(frame)
            if bcode.startswith("CALL_FUNCTION"):
                value_stack = ValueStack(frame)
                function = value_stack.bottom()
                # Python functions are handled by the standard trace mechanism, but
                # we have to make sure any C calls the function makes can be traced
                # by us later, so we rewrite its bytecode.
                if not is_c_func(function):
                    rewrite_function(function)
                    return
                self.call_stack.append(value_stack.offset)
                pargs = value_stack.positional_args()
                kargs = value_stack.keyword_args()
                # Rewrite all callables that may have been passed to the C function.
                rewrite_all(pargs + kargs.values())
                return 'c_call', (function, pargs, kargs)
            elif self.call_stack[-1] is not None:
                offset = self.call_stack.pop()
                return 'c_return', get_value_stack(frame)[offset]
            elif bcode.startswith("PRINT_"):
                return 'print', None # TODO
        elif event == 'call':
            self.call_stack.append(None)
        # When an exception happens in Python code, 'exception' and 'return' events
        # are reported in succession. Exceptions raised from C functions don't
        # generate the 'return' event, so we have to pop from the stack right away.
        elif event == 'exception' and self.call_stack[-1] is not None:
            self.call_stack.pop()
        # Python functions always generate a 'return' event, even when an exception
        # has been raised, so let's just check for that.
        elif event == 'return':
            self.call_stack.pop()

def rewrite_lnotab(code):
    """Replace a code object's line number information to claim that every
    byte of the bytecode is a new line. Returns a new code object.
    Also recurses to hack the line numbers in nested code objects.

    Based on Ned Batchelder's hackpyc.py:
      http://nedbatchelder.com/blog/200804/wicked_hack_python_bytecode_tracing.html
    """
    if has_been_rewritten(code):
        return
    n_bytes = len(code.co_code)
    new_lnotab = "\x01\x01" * (n_bytes-1)
    new_consts = []
    for const in code.co_consts:
        if type(const) is CodeType:
            new_consts.append(rewrite_lnotab(const))
        else:
            new_consts.append(const)
    return CodeType(code.co_argcount, code.co_nlocals, code.co_stacksize,
        code.co_flags, code.co_code, tuple(new_consts), code.co_names,
        code.co_varnames, code.co_filename, code.co_name, 0, new_lnotab,
        code.co_freevars, code.co_cellvars)

def rewrite_function(function):
    function.func_code = rewrite_lnotab(function.func_code)

def rewrite_all(objects):
    for obj in objects:
        if hasattr(obj, 'func_code'):
            rewrite_function(obj)

def has_been_rewritten(code):
    """Return True if the code has been rewritten by rewrite_lnotab already.

    >>> def fun():
    ...     pass
    >>> has_been_rewritten(fun.func_code)
    False
    >>> rewrite_function(fun)
    >>> has_been_rewritten(fun.func_code)
    True
    """
    return re.match(r"\A(\x01\x01)+\Z", code.co_lnotab) is not None
