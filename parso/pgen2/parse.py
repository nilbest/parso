# Copyright 2004-2005 Elemental Security, Inc. All Rights Reserved.
# Licensed to PSF under a Contributor Agreement.

# Modifications:
# Copyright David Halter and Contributors
# Modifications are dual-licensed: MIT and PSF.

"""
Parser engine for the grammar tables generated by pgen.

The grammar table must be loaded first.

See Parser/parser.c in the Python distribution for additional info on
how this parsing engine works.
"""

from parso.python import tokenize


class InternalParseError(Exception):
    """
    Exception to signal the parser is stuck and error recovery didn't help.
    Basically this shouldn't happen. It's a sign that something is really
    wrong.
    """

    def __init__(self, msg, type, value, start_pos):
        Exception.__init__(self, "%s: type=%r, value=%r, start_pos=%r" %
                           (msg, tokenize.tok_name[type], value, start_pos))
        self.msg = msg
        self.type = type
        self.value = value
        self.start_pos = start_pos


class Stack(list):
    def get_tos_nodes(self):
        tos = self[-1]
        return tos[2][1]

    def get_tos_first_tokens(self, grammar):
        tos = self[-1]
        inv_tokens = dict((v, k) for k, v in grammar.tokens.items())
        inv_keywords = dict((v, k) for k, v in grammar.keywords.items())
        dfa, state, nodes = tos

        def check():
            for first in dfa[1]:
                try:
                    yield inv_keywords[first]
                except KeyError:
                    yield tokenize.tok_name[inv_tokens[first]]

        return sorted(check())


class StackNode(object):
    def __init__(self, dfa):
        self.dfa = dfa
        self.nodes = []

    @property
    def nonterminal(self):
        return self.dfa.from_rule

    def __repr__(self):
        return '%s(%s, %s)' % (self.__class__.__name__, self.dfa, self.nodes)


def token_to_ilabel(grammar, type_, value):
    # Map from token to label
    # TODO this is not good, shouldn't use tokenize.NAME, but somehow use the
    # grammar.
    if type_ == tokenize.NAME:
        # Check for reserved words (keywords)
        try:
            return grammar.keywords[value]
        except KeyError:
            pass

    try:
        return grammar.tokens[type_]
    except KeyError:
        return None


class PgenParser(object):
    """Parser engine.

    The proper usage sequence is:

    p = Parser(grammar, [converter])  # create instance
    p.setup([start])                  # prepare for parsing
    <for each input token>:
        if p.add_token(...):           # parse a token
            break
    root = p.rootnode                 # root of abstract syntax tree

    A Parser instance may be reused by calling setup() repeatedly.

    A Parser instance contains state pertaining to the current token
    sequence, and should not be used concurrently by different threads
    to parse separate token sequences.

    See driver.py for how to get input tokens by tokenizing a file or
    string.

    Parsing is complete when add_token() returns True; the root of the
    abstract syntax tree can then be retrieved from the rootnode
    instance variable.  When a syntax error occurs, error_recovery()
    is called. There is no error recovery; the parser cannot be used
    after a syntax error was reported (but it can be reinitialized by
    calling setup()).

    """

    def __init__(self, grammar, convert_node, convert_leaf, error_recovery, start):
        """Constructor.

        The grammar argument is a grammar.Grammar instance; see the
        grammar module for more information.

        The parser is not ready yet for parsing; you must call the
        setup() method to get it started.

        The optional convert argument is a function mapping concrete
        syntax tree nodes to abstract syntax tree nodes.  If not
        given, no conversion is done and the syntax tree produced is
        the concrete syntax tree.  If given, it must be a function of
        two arguments, the first being the grammar (a grammar.Grammar
        instance), and the second being the concrete syntax tree node
        to be converted.  The syntax tree is converted from the bottom
        up.

        A concrete syntax tree node is a (type, nodes) tuple, where
        type is the node type (a token or nonterminal number) and nodes
        is a list of children for nonterminals, and None for tokens.

        An abstract syntax tree node may be anything; this is entirely
        up to the converter function.

        """
        self.grammar = grammar
        self.convert_node = convert_node
        self.convert_leaf = convert_leaf

        start_nonterminal = grammar.number2nonterminal[start]
        self.stack = Stack([StackNode(grammar._nonterminal_to_dfas[start_nonterminal][0])])
        self.rootnode = None
        self.error_recovery = error_recovery

    def parse(self, tokens):
        for type_, value, start_pos, prefix in tokens:
            self.add_token(type_, value, start_pos, prefix)

        while self.stack and self.stack[-1].dfa.is_final:
            self._pop()

        if self.stack:
            # We never broke out -- EOF is too soon -- Unfinished statement.
            # However, the error recovery might have added the token again, if
            # the stack is empty, we're fine.
            raise InternalParseError("incomplete input", type_, value, start_pos)
        return self.rootnode

    def add_token(self, type_, value, start_pos, prefix):
        """Add a token; return True if this is the end of the program."""
        ilabel = token_to_ilabel(self.grammar, type_, value)
        stack = self.stack
        grammar = self.grammar

        while True:
            try:
                plan = stack[-1].dfa.ilabel_to_plan[ilabel]
                break
            except KeyError:
                if stack[-1].dfa.is_final:
                    self._pop()
                else:
                    self.error_recovery(grammar, stack, type_,
                                        value, start_pos, prefix, self.add_token)
                    return
            except IndexError:
                raise InternalParseError("too much input", type_, value, start_pos)

        stack[-1].dfa = plan.next_dfa

        for push in plan.dfa_pushes:
            stack.append(StackNode(push))

        leaf = self.convert_leaf(grammar, type_, value, prefix, start_pos)
        stack[-1].nodes.append(leaf)

    def _pop(self):
        tos = self.stack.pop()
        # If there's exactly one child, return that child instead of
        # creating a new node.  We still create expr_stmt and
        # file_input though, because a lot of Jedi depends on its
        # logic.
        if len(tos.nodes) == 1:
            new_node = tos.nodes[0]
        else:
            new_node = self.convert_node(self.grammar, tos.dfa.from_rule, tos.nodes)

        try:
            self.stack[-1].nodes.append(new_node)
        except IndexError:
            # Stack is empty, set the rootnode.
            self.rootnode = new_node
