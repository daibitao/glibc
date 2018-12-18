#!/usr/bin/python3
# Copyright (C) 2018 Free Software Foundation, Inc.
# This file is part of the GNU C Library.
#
# The GNU C Library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# The GNU C Library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with the GNU C Library; if not, see
# <http://www.gnu.org/licenses/>.
''' Generate a ChangeLog style output based on the git log.

This script takes two revisions as input and generates a ChangeLog style output
for all revisions between the two revisions.  This output is intended to be an
approximation and not the exact ChangeLog.

At a high level, the script enumerates all C source files (*.c and *.h) and
builds a tree of top level objects within macro conditionals.  The top level
objects the script currently attempts to identify are:

    - Include statements
    - Macro definitions and undefs
    - Declarations and definitions of variables and functions
    - Composite types

The script attempts to identify quirks typically used in glibc sources such as
the symbol hack macro calls that don't use a semicolon and tries to adjust for
them.

Known Limitations:

    - Does not identify changes in or to comments.  Comments are simply stripped
      out.
    - Weird nesting of macro conditionals may break things.  Attempts have been
      made to try and maintain state across macro conditional scopes, but
      there's still scope to fool the script.
    - Does not identify changes within functions.
'''
import subprocess
import sys
import os
import re
from enum import Enum

# General Utility functions.
def eprint(*args, **kwargs):
    ''' Print to stderr.
    '''
    print(*args, file=sys.stderr, **kwargs)


debug = False
def debug_print(*args, **kwargs):
    ''' Convenience function to print diagnostic information in the program.
    '''
    if debug:
        eprint(*args, **kwargs)


def usage(name):
    ''' Print program usage.
    '''
    eprint("usage: %s <from-ref> <to-ref>" % name)
    sys.exit(os.EX_USAGE)


def decode(string):
    ''' Attempt to decode a string.

    Decode a string read from the source file.  The multiple attempts are needed
    due to the presence of the page break characters and some tests in locales.
    '''
    codecs = ['utf8', 'latin1', 'cp1252']

    for i in codecs:
        try:
            return string.decode(i)
        except UnicodeDecodeError:
            pass

    eprint('Failed to decode: %s' % string)


def new_block(name, type, contents, parent):
    '''  Create a new code block with the parent as PARENT.

    The code block is a basic structure around which the tree representation of
    the source code is built.  It has the following attributes:

    - type: Any one of the following types in BLOCK_TYPE.
    - name: A name to refer it by in the ChangeLog
    - contents: The contents of the block.  For a block of types file or
      macro_cond, this would be a list of blocks that it nests.  For other types
      it is a list with a single string specifying its contents.
    - parent: This is the parent of the current block, useful in setting up
      #elif or #else blocks in the tree.
    - matched: A flag to specify if the block in a tree has found a match in the
      other tree to which it is being compared.
    '''
    block = {}
    block['matched'] = False
    block['name'] = name
    block['type'] = type
    block['contents'] = contents
    block['parent'] = parent
    if parent:
        parent['contents'].append(block)

    return block


class block_type(Enum):
    ''' Type of code block.
    '''
    file = 1
    macro_cond = 2
    macro_def = 3
    macro_undef = 4
    macro_include = 5
    macro_info = 6
    decl = 7
    func = 8
    composite = 9
    macrocall = 10
    fndecl = 11
    assign = 12


#------------------------------------------------------------------------------
# C Parser.
#------------------------------------------------------------------------------
# Regular expressions.

# The __attribute__ are written in a bunch of different ways in glibc.
ATTRIBUTE = \
        r'((_*(attribute|ATTRIBUTE)_*(\s*\(\([^)]+\)\)|\w+))|weak_function)';

# Function regex
FUNC_RE = re.compile(ATTRIBUTE + r'*\s*(\w+)\s*\([^(][^{]+\)\s*{')

# The macrocall_re peeks into the next line to ensure that it doesn't eat up
# a FUNC by accident.  The func_re regex is also quite crude and only
# intends to ensure that the function name gets picked up correctly.
MACROCALL_RE = re.compile(r'(\w+)\s*\(\w+(\s*,\s*[\w\.]+)*\)$')

# Composite types such as structs and unions.
COMPOSITE_RE = re.compile(r'(struct|union|enum)\s*(\w*)\s*{')

# Static assignments.
ASSIGN_RE = re.compile(r'(\w+)\s*(\[[^\]]*\])?\s*([^\s]*attribute[\s\w()]+)?\s*=')

# Function Declarations.
FNDECL_RE = re.compile(r'(\w+)\s*\([^;]+\)\s*' + ATTRIBUTE + '*;')

# Function pointer typedefs.
TYPEDEF_FN_RE = re.compile(r'\(\*(\w+)\)\s*\([^)]+\);')

# Simple decls.
DECL_RE = re.compile(r'(\w+)(\[\w+\])?\s*' + ATTRIBUTE + '?;')


def remove_comments(op):
    ''' Remove comments.

    Return OP by removing all comments from it.
    '''
    debug_print('REMOVE COMMENTS')

    sep='\n'
    opstr = sep.join(op)
    opstr = re.sub(r'/\*.*?\*/', r'', opstr, flags=re.MULTILINE | re.DOTALL)
    opstr = re.sub(r'\\\n', r' ', opstr, flags=re.MULTILINE | re.DOTALL)
    new_op = list(filter(None, opstr.split(sep)))

    return new_op


# Parse macros.
def parse_preprocessor(op, loc, code, start = '', else_start = ''):
    ''' Parse a preprocessor directive.

    In case a preprocessor condition (i.e. if/elif/else), create a new code
    block to nest code into and in other cases, identify and add entities suchas
    include files, defines, etc.

    - NAME is the name of the directive
    - CUR is the string to consume this expression from
    - OP is the string array for the file
    - LOC is the first unread location in CUR
    - CODE is the block to which we add this function

    - Returns: The next location to be read in the array.
    '''
    cur = op[loc]
    loc = loc + 1
    endblock = False

    debug_print('PARSE_MACRO: %s' % cur)

    # Remove the # and strip spaces again.
    cur = cur[1:]

    # Include file.
    if cur.find('include') == 0:
        m = re.search(r'include\s*["<]?([^">]+)[">]?', cur)
        new_block(m.group(1), block_type.macro_include, [cur], code)

    # Macro definition.
    if cur.find('define') == 0:
        m = re.search(r'define\s+([a-zA-Z0-9_]+)', cur)
        new_block(m.group(1), block_type.macro_def, [cur], code)

    # Macro undef.
    if cur.find('undef') == 0:
        m = re.search(r'undef\s+([a-zA-Z0-9_]+)', cur)
        new_block(m.group(1), block_type.macro_undef, [cur], code)

    # #error and #warning macros.
    if cur.find('error') == 0 or cur.find('warning') == 0:
        m = re.search(r'(error|warning)\s+"?(.*)"?', cur)
        if m:
            name = m.group(2)
        else:
            name = '<blank>'
        new_block(name, block_type.macro_info, [cur], code)

    # Start of an #if or #ifdef block.
    elif cur.find('if') == 0:
        rem = re.sub(r'ifndef', r'!', cur).strip()
        rem = re.sub(r'(ifdef|defined|if)', r'', rem).strip()
        ifdef = new_block(rem, block_type.macro_cond, [], code)
        loc = parse(op, loc, ifdef, start)

    # End the previous #if/#elif and begin a new block.
    elif cur.find('elif') == 0 and code['parent']:
        rem = re.sub(r'(elif|defined)', r'', cur).strip()
        # The #else and #elif blocks should go into the current block's parent.
        ifdef = new_block(rem, block_type.macro_cond, [], code['parent'])
        loc = parse(op, loc, ifdef, else_start)
        endblock = True

    # End the previous #if/#elif and begin a new block.
    elif cur.find('else') == 0 and code['parent']:
        name = '!(' + code['name'] + ')'
        ifdef = new_block(name, block_type.macro_cond, [], code['parent'])
        loc = parse(op, loc, ifdef, else_start)
        endblock = True

    elif cur.find('endif') == 0 and code['parent']:
        endblock = True

    return (loc, endblock)


# Given the start of a scope CUR, lap up all code up to the end of scope
# indicated by the closing brace.
def fast_forward_scope(cur, op, loc):
    ''' Consume lines in a code block.

    Consume all lines of a block of code such as a composite type declaration or
    a function declaration.

    - CUR is the string to consume this expression from
    - OP is the string array for the file
    - LOC is the first unread location in CUR

    - Returns: The next location to be read in the array as well as the updated
      value of CUR, which will now have the body of the function or composite
      type.
    '''
    nesting = cur.count('{') - cur.count('}')
    while nesting > 0 and loc < len(op):
        cur = cur + ' ' + op[loc]

        nesting = nesting + op[loc].count('{')
        nesting = nesting - op[loc].count('}')
        loc = loc + 1

    return (cur, loc)


# Different types of declarations.
def parse_decl(name, cur, op, loc, code, blocktype):
    ''' Parse a top level declaration.

    All types of declarations except function declarations.

    - NAME is the name of the declarated entity
    - CUR is the string to consume this expression from
    - OP is the string array for the file
    - LOC is the first unread location in CUR
    - CODE is the block to which we add this function

    - Returns: The next location to be read in the array.
    '''
    debug_print('FOUND DECL: %s' % name)
    new_block(name, blocktype, [cur], code)

    return loc


# Assignments.
def parse_assign(name, cur, op, loc, code, blocktype):
    ''' Parse an assignment statement.

    This includes array assignments.

    - NAME is the name of the assigned entity
    - CUR is the string to consume this expression from
    - OP is the string array for the file
    - LOC is the first unread location in CUR
    - CODE is the block to which we add this

    - Returns: The next location to be read in the array.
    '''
    debug_print('FOUND ASSIGN: %s' % name)
    # Lap up everything up to semicolon.
    while ';' not in cur and loc < len(op):
        cur = op[loc]
        loc = loc + 1

    new_block(name, blocktype, [cur], code)

    return loc


def parse_composite(name, cur, op, loc, code, blocktype):
    ''' Parse a composite type.

    Match declaration of a composite type such as a sruct or a union..

    - NAME is the name of the composite type
    - CUR is the string to consume this expression from
    - OP is the string array for the file
    - LOC is the first unread location in CUR
    - CODE is the block to which we add this

    - Returns: The next location to be read in the array.
    '''
    if not name:
        name = '<anonymous>'

    # Lap up all of the struct definition.
    (cur, loc) = fast_forward_scope(cur, op, loc)

    new_block(name, blocktype, [cur], code)

    return loc


def parse_func(name, cur, op, loc, code, blocktype):
    ''' Parse a function.

    Match a function definition.

    - NAME is the name of the function
    - CUR is the string to consume this expression from
    - OP is the string array for the file
    - LOC is the first unread location in CUR
    - CODE is the block to which we add this

    - Returns: The next location to be read in the array.
    '''
    debug_print('FOUND FUNC: %s' % name)

    # Consume everything up to the ending brace of the function.
    (cur, loc) = fast_forward_scope(cur, op, loc)

    new_block(name, blocktype, [cur], code)

    return loc


def parse_macrocall(name, cur, op, loc, code, blocktype):
    ''' Parse a macro call.

    Match a symbol hack macro calls that get added without semicolons.

    - NAME is the name of the macro call
    - CUR is the string array for the file
    - CUR is the string to consume this expression from
    - OP is the string array for the file
    - CODE is the block to which we add this

    - Returns: The next location to be read in the array.
    '''
    debug_print('FOUND MACROCALL: %s' % name)

    new_block(name, blocktype, [cur], code)

    return loc


c_expr_parsers = [
        {'regex' : COMPOSITE_RE, 'func' : parse_composite, 'name' : 2,
            'type' : block_type.composite},
        {'regex' : ASSIGN_RE, 'func' : parse_assign, 'name' : 1,
            'type' : block_type.assign},
        {'regex' : TYPEDEF_FN_RE, 'func' : parse_decl, 'name' : 1,
            'type' : block_type.decl},
        {'regex' : FNDECL_RE, 'func' : parse_decl, 'name' : 1,
            'type' : block_type.fndecl},
        {'regex' : FUNC_RE, 'func' : parse_func, 'name' : 5,
            'type' : block_type.func},
        {'regex' : MACROCALL_RE, 'func' : parse_macrocall, 'name' : 1,
            'type' : block_type.macrocall},
        {'regex' : DECL_RE, 'func' : parse_decl, 'name' : 1,
            'type' : block_type.decl}]


def parse_c_expr(cur, op, loc, code):
    ''' Parse a C expression.

    CUR is the string to be parsed, which continues to grow until a match is
    found.  OP is the string array and LOC is the first unread location in the
    string array.  CODE is the block in which any identified expressions should
    be added.
    '''
    debug_print('PARSING: %s' % cur)

    # TODO: There's probably a quicker way to do this.
    for p in c_expr_parsers:
        found = re.search(p['regex'], cur)
        if found:
            return '', p['func'](found.group(p['name']), cur, op, loc, code,
                                    p['type'])

    return cur, loc


def parse(op, loc, code, start = ''):
    '''
    Parse the file line by line.  The function assumes a mostly GNU coding
    standard compliant input so it might barf with anything that is eligible for
    the Obfuscated C code contest.

    The basic idea of the parser is to identify macro conditional scopes and
    definitions, includes, etc. and then parse the remaining C code in the
    context of those macro scopes.  The parser does not try to understand the
    semantics of the code or even validate its syntax.  It only records high
    level symbols in the source and makes a tree structure to indicate the
    declaration/definition of those symbols and their scope in the macro
    definitions.

    LOC is the first unparsed line.
    '''
    cur = start
    endblock = False

    while loc < len(op):
        nextline = op[loc]

        # Macros.
        if nextline[0] == '#':
            (loc, endblock) = parse_preprocessor(op, loc, code, cur, start)
            if endblock and not cur:
                return loc
        # Rest of C Code.
        else:
            cur = cur + ' ' + nextline
            cur, loc = parse_c_expr(cur, op, loc + 1, code)

    return loc


def parse_output(op):
    ''' File parser.

    Parse the input array of lines OP and generate a tree structure to
    represent the file.  This tree structure is then used for comparison between
    the old and new file.
    '''
    tree = new_block('', block_type.file, [], None)
    op = remove_comments(op)
    op = [re.sub(r'#\s+', '#', x) for x in op]
    op = parse(op, 0, tree)

    return tree


def print_tree(tree, indent):
    ''' Print the entire tree.
    '''
    if not debug:
        return

    if tree['type'] == block_type.macro_cond or tree['type'] == block_type.file:
        print('%sScope: %s' % (' ' * indent, tree['name']))
        for c in tree['contents']:
            print_tree(c, indent + 4)
        print('%sEndScope: %s' % (' ' * indent, tree['name']))
    else:
        if tree['type'] == block_type.func:
            print('%sFUNC: %s' % (' ' * indent, tree['name']))
        elif tree['type'] == block_type.composite:
            print('%sCOMPOSITE: %s' % (' ' * indent, tree['name']))
        elif tree['type'] == block_type.assign:
            print('%sASSIGN: %s' % (' ' * indent, tree['name']))
        elif tree['type'] == block_type.fndecl:
            print('%sFNDECL: %s' % (' ' * indent, tree['name']))
        elif tree['type'] == block_type.decl:
            print('%sDECL: %s' % (' ' * indent, tree['name']))
        elif tree['type'] == block_type.macrocall:
            print('%sMACROCALL: %s' % (' ' * indent, tree['name']))
        elif tree['type'] == block_type.macro_def:
            print('%sDEFINE: %s' % (' ' * indent, tree['name']))
        elif tree['type'] == block_type.macro_include:
            print('%sINCLUDE: %s' % (' ' * indent, tree['name']))
        elif tree['type'] == block_type.macro_undef:
            print('%sUNDEF: %s' % (' ' * indent, tree['name']))
        else:
            print('%sMACRO LEAF: %s' % (' ' * indent, tree['name']))

#------------------------------------------------------------------------------


def exec_git_cmd(args):
    ''' Execute a git command and return its result as a list of strings
    '''
    args.insert(0, 'git')
    debug_print(args)
    proc = subprocess.Popen(args, stdout=subprocess.PIPE)

    # Clean up the output by removing trailing spaces, newlines and dropping
    # blank lines.
    op = [decode(x[:-1]).strip() for x in proc.stdout]
    op = [re.sub(r'\s+', ' ', x) for x in op]
    op = [x for x in op if x]
    return op


def print_changed_tree(tree, action, prologue = ''):
    ''' Print the nature of the differences found in the tree compared to the
    other tree.  TREE is the tree that changed, action is what the change was
    (Added, Removed, Modified) and prologue specifies the macro scope the change
    is in.  The function calls itself recursively for all macro condition tree
    nodes.
    '''

    if tree['type'] != block_type.macro_cond:
        print('\t%s(%s): %s.' % (prologue, tree['name'], action))
        return

    prologue = '%s[%s]' % (prologue, tree['name'])
    for t in tree['contents']:
        if t['type'] == block_type.macro_cond:
            print_changed_tree(t, action, prologue)
        else:
            print('\t%s(%s): %s.' % (prologue, t['name'], action))


def compare_trees(left, right, prologue = ''):
    ''' Compare two trees and print the difference.

    This routine is the entry point to compare two trees and print out their
    differences.  LEFT and RIGHT will always have the same name and type,
    starting with block_type.file and '' at the top level.
    '''

    if left['type'] == block_type.macro_cond or left['type'] == block_type.file:

        if left['type'] == block_type.macro_cond:
            prologue = '%s[%s]' % (prologue, left['name'])

        # TODO 1: There must be some list comprehension magic I can do here.
        # TODO 2: This won't detect when the macro condition has been changed.
        # It will think of one condition as added and another as removed.  We'll
        # have to live with that for now.

        # Make sure that everything in the left tree exists in the right tree.
        for cl in left['contents']:
            found = False
            for cr in right['contents']:
                if not cl['matched'] and not cr['matched'] and \
                        cl['name'] == cr['name'] and cl['type'] == cr['type']:
                    cl['matched'] = cr['matched'] = True
                    compare_trees(cl, cr, prologue)
                    found = True
                    break
            if not found:
                print_changed_tree(cl, 'Removed', prologue)

        # ... and vice versa.  This time we only need to look at unmatched
        # contents.
        for cr in right['contents']:
            if not cr['matched']:
                print_changed_tree(cr, 'New', prologue)
    else:
        if left['contents'] != right['contents']:
            print_changed_tree(left, 'Modified', prologue)


def analyze_diff(oldfile, newfile, filename):
    ''' Parse the output of the old and new files and print the difference.

    For input files OLDFILE and NEWFILE with name FILENAME, generate reduced
    trees for them and compare them.  We limit our comparison to only C source
    files.
    '''
    split = filename.split('.')
    ext = ''
    if split:
        ext = split[-1]

    if ext != 'c' and ext != 'h':
        return

    debug_print('\t<List diff between oldfile and newfile>')
    # op = exec_git_cmd(['diff', '-U20000', oldfile, newfile])
    # (left, right) = parse_output(op)

    left = parse_output(exec_git_cmd(['show', oldfile]))
    right = parse_output(exec_git_cmd(['show', newfile]))

    compare_trees(left, right)

    debug_print('LEFT TREE')
    debug_print('-' * 80)
    print_tree(left, 0)
    debug_print('RIGHT TREE')
    debug_print('-' * 80)
    print_tree(right, 0)


def list_changes(commit):
    ''' List changes in a single commit.

    For the input commit id COMMIT, identify the files that have changed and the
    nature of their changes.  Print commit information in the ChangeLog format,
    calling into helper functions as necessary.
    '''

    op = exec_git_cmd(['show', '--date=short', '--raw', commit])
    author = ''
    date = ''
    merge = False

    for l in op:
        if l.find('Author:') == 0:
            tmp=l[7:].split('<')
            authorname = tmp[0].strip()
            authoremail=tmp[1][:-1].strip()
        elif l.find('Date:') == 0:
            date=l[5:].strip()
        elif l.find('Merge:') == 0:
            merge = True

        # We got Author and Date, so don't bother with the remaining output.
        if author != '' and date != '':
            break

    # Find raw commit information for all non-ChangeLog files.
    op = [x[1:] for x in op if len(x) > 0 and re.match(r'^:[0-9]+', x) \
            and 'ChangeLog' not in x]

    # It was only the ChangeLog, ignore.
    if len(op) == 0:
        return

    print('%s  %s  <%s>\n' % (date, authorname, authoremail))

    if merge:
       print('\t MERGE COMMIT: %s\n' % commit)
       return

    print('\tCOMMIT: %s' % commit)

    # Each of these lines has a space separated format like so:
    # :<OLD MODE> <NEW MODE> <OLD REF> <NEW REF> <OPERATION> <FILE1> <FILE2>
    #
    # where OPERATION can be one of the following:
    # A: File added
    # D: File removed
    # M[0-9]{3}: File modified
    # R[0-9]{3}: File renamed, with the 3 digit number following it indicating
    # what percentage of the file is intact.
    # C[0-9]{3}: File copied.  Same semantics as R.
    # T: The permission bits of the file changed
    # U: Unmerged.  We should not encounter this, so we ignore it/
    # X, or anything else: Most likely a bug.  Report it.
    #
    # FILE2 is set only when OPERATION is R or C, to indicate the new file name.
    #
    # Also note that merge commits have a different format here, with three
    # entries each for the modes and refs, but we don't bother with it for now.
    #
    # For more details: https://git-scm.com/docs/diff-format
    for f in op:
        data = f.split()
        if data[4] == 'A':
            print('\t* %s: New file.' % data[5])
        elif data[4] == 'D':
            print('\t* %s: Delete file.' % data[5])
        elif data[4] == 'T':
            print('\t* %s: Changed file permission bits from %s to %s' % \
                    (data[5], data[0], data[1]))
        elif data[4][0] == 'M':
            print('\t* %s: Modified.' % data[5])
            analyze_diff(data[2], data[3], data[5])
        elif data[4][0] == 'R' or data[4][0] == 'C':
            change = int(data[4][1:])
            print('\t* %s: Move to...' % data[5])
            print('\t* %s: ... here.' % data[6])
            if change < 100:
                analyze_diff(data[2], data[3], data[6])
        # We should never encounter this, so ignore for now.
        elif data[4] == 'U':
            pass
        else:
            eprint('%s: Unknown line format %s' % (commit, data[4]))
            sys.exit(42)

    print('')


def list_commits(revs):
    ''' List commit IDs between the two revs in the REVS list.
    '''
    ref = revs[0] + '..' + revs[1]
    return exec_git_cmd(['log', '--pretty=%H', ref])


def main(revs):
    ''' ChangeLog Generator Entry Point
    '''
    commits = list_commits(revs)
    for commit in commits:
        list_changes(commit)


def parser_file_test(f):
    ''' Parser debugger Entry Point
    '''
    with open(f) as srcfile:
        op = srcfile.readlines()
        op = [x[:-1] for x in op]
        tree = parse_output(op)
        print_tree(tree, 0)


# Program Entry point.  If -d is specified, the second argument is assumed to be
# a file and only the parser is run in verbose mode.
if __name__ == '__main__':
    if len(sys.argv) != 3:
        usage(sys.argv[0])

    if sys.argv[1] == '-d':
        debug = True
        parser_file_test(sys.argv[2])
    else:
        main(sys.argv[1:])
