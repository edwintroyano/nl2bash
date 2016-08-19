#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
This file augments the AST generated by bashlex with single-command architecture and constraints.
It also performs some normalization on the command arguments.
"""

from __future__ import print_function
import json
import os
import re
import sys
sys.path.append("../grammar")

# bashlex stuff
import bast, errors, tokenizer, bparser
import bash

# TODO: add stdin & stdout types
simplified_bash_syntax = [
    "Command ::= SingleCommand | Pipe",
    "Pipe ::= Command '|' Command",
    "SingleCommand ::= HeadCommand [OptionList]",
    "OptionList ::= Option | OptionList",
    "Option ::= Flag [Argument] | LogicOp Option",
    "Argument ::= SingleArgument | CommandSubstitution | ProcessSubstitution",
    "CommandSubstitution ::= ` Command `",
    "ProcessSubstitution ::= <( Command ) | >( Command )"
]

arg_syntax = [
    "File",
    "Pattern",
    "Number",
    "NumberExp ::= -Number | +Number",
    "SizeExp ::= Number(k) | Number(M) | Number(G) | Number(T) | Number(P)",
    "TimeExp ::= Number(s) | Number(m) | Number(h) | Number(d) | Number(w)",
    # TODO: add fine-grained permission pattern
    "PermissionMode",
    "UserName",
    "GroupName",
    "Unknown"
]

unary_logic_operators = set(['!', '-not'])

binary_logic_operators = set([
    '-and',
    '-or',
    '||',
    '&&',
    '-o',
    '-a'
])

def is_unary_logic_op(node, parent):
    if node.word == "!":
        return parent and parent.kind == "headcommand" and parent.value == "find"
    return node.word in unary_logic_operators

def is_binary_logic_op(node, parent):
    if node.word == '-o':
        if parent and parent.kind == "headcommand" and parent.value == "find":
            node.word = "-or"
            return True
        else:
            return False
    if node.word == '-a':
        if parent and parent.kind == "headcommand" and parent.value == "find":
            node.word = "-and"
            return True
        else:
            return False
    return node.word in binary_logic_operators

def all_simple_commands(ast):
    """Check if an ast contains only high-frequency commands."""
    node = ast
    if node.type == "headcommand" and not node in bash.head_commands:
        return False
    for child in node.children:
        if not all_simple_commands(child):
            return False
    return True

class Node(object):
    num_child = -1              # default value = -1, allow arbitrary number of children
    children_types = []         # list of children types
                                # a length-one list of representing the common types for each
                                # child if num_child = -1
                                # dummy field if num_child = 0

    def __init__(self, parent=None, lsb=None, kind="", value=""):
        """
        :member kind: ['pipe',
                      'headcommand',
                      'logicop',
                      'flag',
                      'file', 'pattern', 'numberexp',
                      'sizeexp', 'timeexp', 'permexp',
                      'username', 'groupname', 'unknown',
                      'number', 'unit', 'op',
                      'commandsubstitution',
                      'processsubstitution'
                     ]
        :member value: string value of the node
        :member parent: pointer to parent node
        :member lsb: pointer to left sibling node
        :member children: list of child nodes
        """
        self.parent = parent
        self.lsb = lsb
        self.rsb = None
        self.kind = kind
        self.value = value
        self.children = []

    def addChild(self, child):
        self.children.append(child)

    def getNumChildren(self):
        return len(self.children)

    def getRightChild(self):
        if len(self.children) >= 1:
            return self.children[-1]
        else:
            return None

    def getSecond2RightChild(self):
        if len(self.children) >= 2:
            return self.children[-2]
        else:
            return None

    def is_flag(self):
        return bash.is_option(self.value)

    def removeChild(self, child):
        self.children.remove(child)

    def removeChildByIndex(self, index):
        self.children.pop(index)

    @property
    def symbol(self):
        return self.kind.upper() + "_" + self.value

# syntax constraints for different kind of nodes
class ArgumentNode(Node):
    num_child = 0

    def __init__(self, kind="", value="", parent=None, lsb=None):
        super(ArgumentNode, self).__init__(parent, lsb, kind, value)

class UnaryLogicOpNode(Node):
    num_child = 1
    children_types = [set('flag')]

    def __init__(self, value="", parent=None, lsb=None):
        super(UnaryLogicOpNode, self).__init__( parent, lsb, 'unarylogicop', value)

class BinaryLogicOpNode(Node):
    num_child = 2
    children_types = [set('flag'), set('flag')]

    def __init__(self, value="", parent=None, lsb=None):
        super(BinaryLogicOpNode, self).__init__(parent, lsb, 'binarylogicop', value)

class PipelineNode(Node):
    children_types = [set(['headcommand'])]

    def __init__(self, parent=None, lsb=None):
        super(PipelineNode, self).__init__(parent, lsb)
        self.kind = 'pipeline'

class CommandSubstitutionNode(Node):
    num_child = 1
    children_types = [set(['pipe', 'headcommand'])]

    def __init__(self, parent=None, lsb=None):
        super(CommandSubstitutionNode, self).__init__(parent, lsb)
        self.kind = "commandsubstitution"

class ProcessSubstitutionNode(Node):
    num_child = 1
    children_types = [set(['pipe', 'headcommand'])]

    def __init__(self, value, parent=None, lsb=None):
        super(ProcessSubstitutionNode, self).__init__(parent, lsb)
        self.kind = "processsubstitution"
        if value in ["<", ">"]:
            self.value = value
        else:
            raise ValueError("Value of a processsubstitution has to be '<' or '>'.")

def pretty_print(node, depth):
    print("    " * depth + node.kind.upper() + '(' + node.value + ')')
    for child in node.children:
        pretty_print(child, depth+1)

def to_list(node, order='dfs', list=None):
    # linearize the tree for training
    if order == 'dfs':
        list.append(node.symbol)
        for child in node.children:
            to_list(child, order, list)
        list.append("<NO_EXPAND>")
    return list

def to_command(node, loose_constraints=False):
    cmd = ""
    lc = loose_constraints
    # convert a tree to bash command format
    if node.kind == "root":
        assert(loose_constraints or node.getNumChildren() == 1)
        if lc:
            for child in node.children:
                cmd += to_command(child, lc) + ' '
        else:
            cmd = to_command(node.children[0], lc)
    elif node.kind == "pipeline":
        assert(loose_constraints or node.getNumChildren() > 1)
        if lc and node.getNumChildren() < 1:
            cmd += "|"
        elif lc and node.getNumChildren() == 1:
            # treat "single-pipe" as atomic command
            cmd += to_command(node.children[0], lc)
        else:
            for child in node.children[:-1]:
                cmd += to_command(child, lc)
                cmd += " | "
            cmd += to_command(node.children[-1], lc)
    elif node.kind == "commandsubstitution":
        assert(loose_constraints or node.getNumChildren() == 1)
        if lc and node.getNumChildren() < 1:
            cmd = "$(" + ")"
        else:
            cmd = "$(" + to_command(node.children[0], lc) + ")"
    elif node.kind == "processsubstitution":
        assert(loose_constraints or node.getNumChildren() == 1)
        if lc and node.getNumChildren() < 1:
            cmd = node.value + "()"
        else:
            cmd = node.value + "(" + to_command(node.children[0], lc) + ")"
    elif node.kind == "headcommand":
        cmd = node.value + ' '
        for child in node.children:
            cmd += to_command(child, lc) + ' '
        cmd = cmd.strip()
    elif node.kind == "flag":
        cmd = node.value + ' '
        for child in node.children:
            cmd += to_command(child, lc) + ' '
        cmd = cmd.strip()
    elif node.kind == "binarylogicop":
        assert(loose_constraints or node.getNumChildren() == 2)
        if lc and node.getNumChildren() < 2:
            for child in node.children:
                cmd += to_command(child, lc) + ' '
        else:
            cmd += to_command(node.children[0], lc) + ' '
            cmd += node.value + ' '
            cmd += to_command(node.children[1], lc) + ' '
            cmd = cmd.strip()
    elif node.kind == "unarylogicop":
        assert(loose_constraints or node.getNumChildren() == 1)
        if lc and node.getNumChildren() < 1:
            cmd = node.value
        else:
            cmd += node.value + ' '
            cmd += to_command(node.children[0], lc) + ' '
            cmd = cmd.strip()
    elif node.kind == "argument":
        assert(loose_constraints or node.getNumChildren() == 0)
        if lc:
            cmd = node.value + ' '
            for child in node.children:
                cmd += to_command(child, lc) + ' '
            cmd = cmd.strip()
        else:
            cmd = node.value
    return cmd

def list_to_tree(list, order='dfs'):
    # construct a tree from linearized input
    root = Node(kind="root", value="root")
    current = root
    if order == 'dfs':
        for i in xrange(1, len(list)):
            if not current:
                break
            symbol = list[i]
            if symbol == "<NO_EXPAND>":
                current = current.parent
            else:
                kind, value = symbol.split('_', 1)
                kind = kind.lower()
                node = Node(kind=kind, value=value)
                attach_to_tree(node, current)
                current = node
    else:
        raise NotImplementedError
    return root

def special_command_normalization(cmd):
    # special normalization for certain commands
    ## the first argument of "tar" is always interpreted as an option
    tar_fix = re.compile(' tar \w')
    if cmd.startswith('tar'):
        cmd = ' ' + cmd
        for w in re.findall(tar_fix, cmd):
            cmd = cmd.replace(w, w.replace('tar ', 'tar -'))
        cmd = cmd.strip()
    return cmd

def attach_to_tree(node, parent):
    node.parent = parent
    node.lsb = parent.getRightChild()
    parent.addChild(node)
    if node.lsb:
        node.lsb.rsb = node

def make_sibling(lsb, rsb):
    if lsb:
        lsb.rsb = rsb
    if rsb:
        rsb.lsb = lsb

def normalize_ast(cmd, normalize_digits=True, recover_quotation=True):
    """
    Convert the bashlex parse tree of a command into the normalized form.
    :param cmd: bash command to parse
    :param normalize_digits: replace all digits in the tree with the special _NUM symbol
    :param recover_quotation: if set, retain quotation marks in the command
    :return normalized_tree
    """

    cmd = cmd.replace('\n', ' ').strip()
    cmd = special_command_normalization(cmd)

    if not cmd:
        return None

    def find_flag_attach_point(node, attach_point):
        if attach_point.kind == "flag":
            return attach_point.parent
        elif attach_point.kind == "headcommand":
            return attach_point
        else:
            raise ValueError("Error: cannot decide where to attach flag node")

    def normalize_word(node, norm_digit, recover_quote):
        w = recover_quotation(node) if recover_quote else node.word
        return re.sub(bash._DIGIT_RE, bash._NUM, w) if norm_digit and not bash.is_option(w) else w

    def with_quotation(node):
        return (node.pos[1] - node.pos[0] - len(node.word)) == 2

    def recover_quotation(node):
        if with_quotation(node):
            return cmd[node.pos[0] : node.pos[1]]
        else:
            return node.word

    def normalize_command(node, current):
        attach_point = current

        END_OF_OPTIONS = False
        END_OF_COMMAND = False

        unary_logic_ops = []
        binary_logic_ops = []

        def attach_option(node, attach_point):
            attach_point = find_flag_attach_point(node, attach_point)
            normalize(node, attach_point, "flag")
            attach_point = attach_point.getRightChild()
            return attach_point

        # normalize atomic command
        for child in node.parts:
            if END_OF_COMMAND:
                attach_point = attach_point.parent
                if attach_point.kind == "flag":
                    attach_point = attach_point.parent
                elif attach_point.kind == "headcommand":
                    pass
                else:
                    raise ValueError('Error: compound command detected.')   # mostly caused by unknown head commands
                END_OF_COMMAND = False
            if child.kind == 'word':
                if child.word == "--":
                    END_OF_OPTIONS = True
                elif child.word == ";":
                    # handle end of utility introduced by '-exec' and whatnots
                    END_OF_COMMAND = True
                elif child.word in unary_logic_operators:
                    attach_point = find_flag_attach_point(child, attach_point)
                    if is_unary_logic_op(child, attach_point):
                        norm_node = UnaryLogicOpNode(child.word)
                        attach_to_tree(norm_node, attach_point)
                        unary_logic_ops.append(norm_node)
                    else:
                        attach_point = attach_option(child, attach_point)
                elif child.word in binary_logic_operators:
                    attach_point = find_flag_attach_point(child, attach_point)
                    if is_binary_logic_op(child, attach_point):
                        norm_node = BinaryLogicOpNode(child.word)
                        attach_to_tree(norm_node, attach_point)
                        binary_logic_ops.append(norm_node)
                    else:
                        attach_point = attach_option(child, attach_point)
                elif bash.is_headcommand(child.word):
                    if not with_quotation(child):
                        normalize(child, attach_point, "headcommand")
                        attach_point = attach_point.getRightChild()
                elif bash.is_option(child.word) and not END_OF_OPTIONS:
                    attach_point = attach_option(child, attach_point)
                else:
                    #TODO: handle fine-grained argument types
                    if attach_point.is_flag() and attach_point.getNumChildren() >= 1:
                        attach_point = attach_point.parent
                    normalize(child, attach_point, "argument")
            elif child.kind == "assignment":
                normalize(child, attach_point, "assignment")
            else:
                print("Error: unknown type of child of CommandNode")
                print(node)
                sys.exit()

        # process logic operators
        for node in unary_logic_ops:
            # change right sibling to child
            rsb = node.rsb
            if rsb == None:
                print("Warning: unary logic operator without a right sibling.")
                print(node.parent)
                return
            make_sibling(node, rsb.rsb)
            node.parent.removeChild(rsb)
            rsb.parent = node
            rsb.lsb = None
            rsb.rsb = None
            node.addChild(rsb)

        for node in binary_logic_ops:
            # change right sibling to Child
            # change left sibling to child
            rsb = node.rsb
            lsb = node.lsb
            if rsb == None or lsb == None:
                print("Error: binary logic operator must have both left and right siblings.")
                print(node.parent)
                sys.exit()
            make_sibling(node, rsb.rsb)
            make_sibling(lsb.lsb, node)
            node.parent.removeChild(rsb)
            node.parent.removeChild(lsb)
            rsb.parent = node
            lsb.parent = node
            make_sibling(lsb, rsb)
            rsb.rsb = None
            lsb.lsb = None
            node.addChild(lsb)
            node.addChild(rsb)

        # 写完程序员彻底SB了.

    def normalize(node, current, arg_type=""):
        # recursively normalize each subtree
        if not type(node) is bast.node:
            raise ValueError('type(node) is not ast.node')
        if node.kind == 'word':
            # assign fine-grained types
            if node.parts:
                # Compound arguments
                # commandsubstitution, processsubstitution, parameter
                if node.parts[0].kind == "processsubstitution":
                    if '>' in node.word:
                        norm_node = ProcessSubstitutionNode('>')
                        attach_to_tree(norm_node, current)
                        for child in node.parts:
                            normalize(child, norm_node)
                    elif '<' in node.word:
                        norm_node = ProcessSubstitutionNode('<')
                        attach_to_tree(norm_node, current)
                        for child in node.parts:
                            normalize(child, norm_node)
                elif node.parts[0].kind == "commandsubstitution":
                    norm_node = CommandSubstitutionNode()
                    attach_to_tree(norm_node, current)
                    for child in node.parts:
                        normalize(child, norm_node)
                elif node.parts[0].kind == "parameter" or \
                    node.parts[0].kind == "tilde":
                    # if not node.parts[0].value.isdigit():
                    value = normalize_word(node, normalize_digits, recover_quotation)
                    norm_node = ArgumentNode(kind=arg_type, value=value)
                    attach_to_tree(norm_node, current)
                else:
                    for child in node.parts:
                        normalize(child, current)
            else:
                value = normalize_word(node, normalize_digits, recover_quotation)
                norm_node = ArgumentNode(kind=arg_type, value=value)
                attach_to_tree(norm_node, current)
        elif node.kind == "pipeline":
            norm_node = PipelineNode()
            attach_to_tree(norm_node, current)
            if len(node.parts) % 2 == 0:
                print("Error: pipeline node must have odd number of parts")
                print(node)
                sys.exit()
            for child in node.parts:
                if child.kind == "command":
                    normalize(child, norm_node)
                elif child.kind == "pipe":
                    pass
                else:
                    raise ValueError("Error: unrecognized type of child of pipeline node")
                    print(node)
        elif node.kind == "list":
            if len(node.parts) > 2:
                # multiple commands, not supported
                raise("Unsupported: list of length >= 2")
            else:
                normalize(node.parts[0], current)
        elif node.kind == "commandsubstitution" or \
             node.kind == "processsubstitution":
            normalize(node.command, current)
        elif node.kind == "command":
            normalize_command(node, current)
        elif hasattr(node, 'parts'):
            for child in node.parts:
                # skip current node
                normalize(child, current)
        elif node.kind == "operator":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "parameter":
            # not supported
            raise ValueError("Unsupported: parameters")
        elif node.kind == "redirect":
            # not supported
            # if node.type == '>':
            #     parse(node.input, tokens)
            #     tokens.append('>')
            #     parse(node.output, tokens)
            # elif node.type == '<':
            #     parse(node.output, tokens)
            #     tokens.append('<')
            #     parse(node.input, tokens)
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "for":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "if":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "while":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "until":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "assignment":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "function":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "tilde":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)
        elif node.kind == "heredoc":
            # not supported
            raise ValueError("Unsupported: %s" % node.kind)

    try:
        tree = bparser.parse(cmd)
    except tokenizer.MatchedPairError, e:
        print("Cannot parse: %s - MatchedPairError" % cmd.encode('utf-8'))
        # return basic_tokenizer(cmd, normalize_digits, False)
        return None
    except errors.ParsingError, e:
        print("Cannot parse: %s - ParsingError" % cmd.encode('utf-8'))
        # return basic_tokenizer(cmd, normalize_digits, False)
        return None
    except NotImplementedError, e:
        print("Cannot parse: %s - NotImplementedError" % cmd.encode('utf-8'))
        # return basic_tokenizer(cmd, normalize_digits, False)
        return None
    except IndexError, e:
        print("Cannot parse: %s - IndexError" % cmd.encode('utf-8'))
        # empty command
        return None
    except AttributeError, e:
        print("Cannot parse: %s - AttributeError" % cmd.encode('utf-8'))
        # not a bash command
        return None

    if len(tree) > 1:
        print("Doesn't support command with multiple root nodes: %s" % cmd.encode('utf-8'))
    normalized_tree = Node(kind="root")
    try:
        normalize(tree[0], normalized_tree)
    except ValueError as err:
        print("%s - %s" % (err.args[0], cmd.encode('utf-8')))
        return None

    return normalized_tree

if __name__ == "__main__":
    cmd = sys.argv[1]
    norm_tree = normalize_ast(cmd, g)
    pretty_print(norm_tree, 0)
    list = to_list(norm_tree, 'dfs', [])
    print(list)
    tree = list_to_tree(list + ['<PAD>'])
    pretty_print(tree, 0)
    print(to_command(tree))



