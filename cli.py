#!/usr/bin/env python3
"""
ZanChop UCC - Command Line Interface
A CLI marketplace for UCC students to buy and sell food.
"""

import os
import sys
import sqlite3
from datetime import datetime
from decimal import Decimal

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

# Fix Windows console encoding for Unicode characters
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Initialize console for rich output
console = Console()

# =========================
# CONFIG & DATABASE
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "prim_store.db")

# UCC Zones & Delivery Fees
UCC_ZONES = {
    "North Campus (Casford/KNH)": 5.0,
    "South Campus (Oguaa/Adehye)": 5.0,
    "Science (Sasakawa/Market)": 6.0,
    "Amamoma": 7.0,
    "Kwaprow": 7.0,
    "Duakor": 8.0
}

def get_db():
    """Get database connection."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

# =========================
# CONTEXT
# =========================
class UserContext:
    """Store current user context."""
    def __init__(self):
        self.phone = None
        self.name = None
        self.role = None
        self.zone = None
        self.is_logged_in = False

pass_user_context = click.make_pass_decorator(UserContext, ensure=True)

# =========================
# CLI GROUP
# =========================
@click.group()
@click.option('--phone', help='Your phone number', envvar='ZANCHOP_PHONE')
@click.option('--debug', is_flag=True, help='Enable debug mode')
@click.pass_context
def cli(ctx, phone, debug):
    """
    🎓 ZanChop UCC - Campus Food Marketplace CLI
    
    Buy and sell food within UCC campus. Register as a buyer or seller,
    browse available food, place orders, and manage your marketplace activity.
    
    Examples:
        zanchop register
        zanchop --phone 233xxxxxxxxx seller add-product
        zanchop --phone 233xxxxxxxxx buyer browse
    """
    ctx.ensure_object(UserContext)
    user_ctx = ctx.obj
    
    if phone:
        user_ctx.phone = phone
        # Try to load user info
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT phone, name, role, zone FROM users WHERE phone = ?", (phone,))
        user = c.fetchone()
        conn.close()
        
        if user:
            user_ctx.phone = user['phone']
            user_ctx.name = user['name']
            user_ctx.role = user['role']
            user_ctx.zone = user['zone']
            user_ctx.is_logged_in = True
            if debug:
                console.print(f"[green]Logged in as {user_ctx.name} ({user_ctx.role})[/green]")

# =========================
# AUTH COMMANDS
# =========================
@cli.command()
@click.option('--name', prompt='Your full name', help='Your full name')
@click.option('--phone', prompt='Your phone number', help='Your phone number (e.g., 233xxxxxxxxx)')
@click.option('--zone', type=click.Choice(list(UCC_ZONES.keys())), prompt='Your zone', help='Your campus zone')
@click.option('--role', type=click.Choice(['buyer', 'seller']), prompt='Register as', help='Your role')
def register(name, phone, zone, role):
    """Register a new user account."""
    conn = get_db()
    c = conn.cursor()
    
    # Check if user already exists
    c.execute("SELECT phone FROM users WHERE phone = ?", (phone,))
    if c.fetchone():
        console.print("[red]❌ Error: Phone number already registered![/red]")
        console.print(f"[yellow]Use --phone {phone} with other commands to access your account.[/yellow]")
        conn.close()
        return
    
    # Create user
    c.execute(
        "INSERT INTO users (phone, name, role, zone) VALUES (?, ?, ?, ?)",
        (phone, name, role, zone)
    )
    conn.commit()
    conn.close()
    
    # Success message
    console.print(Panel(
        f"[green]✅ Registration Successful![/green]\n\n"
        f"[bold]Name:[/bold] {name}\n"
        f"[bold]Phone:[/bold] {phone}\n"
        f"[bold]Zone:[/bold] {zone}\n"
        f"[bold]Role:[/bold] {role.capitalize()}\n\n"
        f"[blue]Use --phone {phone} with commands to access your account.[/blue]",
        title="🎓 ZanChop UCC",
        border_style="green"
    ))
    
    if role == 'seller':
        console.print("\n[cyan]Seller commands:[/cyan]")
        console.print("  zanchop --phone {phone} seller add-product".format(phone=phone))
        console.print("  zanchop --phone {phone} seller my-products".format(phone=phone))
    else:
        console.print("\n[cyan]Buyer commands:[/cyan]")
        console.print("  zanchop --phone {phone} buyer browse".format(phone=phone))
        console.print("  zanchop --phone {phone} buyer orders".format(phone=phone))

@cli.command()
@click.option('--phone', prompt='Phone number', help='Phone number to login')
def login(phone):
    """Check your account status."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT phone, name, role, zone FROM users WHERE phone = ?", (phone,))
    user = c.fetchone()
    conn.close()
    
    if user:
        console.print(Panel(
            f"[bold]Name:[/bold] {user['name']}\n"
            f"[bold]Phone:[/bold] {user['phone']}\n"
            f"[bold]Zone:[/bold] {user['zone']}\n"
            f"[bold]Role:[/bold] {user['role'].capitalize()}\n\n"
            f"[green]✅ Account active![/green]",
            title=f"👤 {user['name']}",
            border_style="blue"
        ))
        console.print(f"\n[blue]Use: zanchop --phone {phone} <command>[/blue]")
    else:
        console.print("[red]❌ Account not found. Please register first.[/red]")
        console.print("[yellow]Run: zanchop register[/yellow]")

# =========================
# SELLER COMMANDS
# =========================
@cli.group()
@click.pass_context
def seller(ctx):
    """Seller commands - Manage your food menu and orders."""
    ctx.ensure_object(UserContext)
    if not ctx.obj.phone:
        console.print("[red]❌ Error: Please provide your phone number with --phone[/red]")
        console.print("[yellow]Example: zanchop --phone 233xxxxxxxxx seller add-product[/yellow]")
        ctx.exit(1)
    
    # Verify user is a seller
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT role FROM users WHERE phone = ?", (ctx.obj.phone,))
    user = c.fetchone()
    conn.close()
    
    if not user:
        console.print("[red]❌ Error: User not found. Please register first.[/red]")
        ctx.exit(1)
    
    if user['role'] != 'seller':
        console.print("[red]❌ Error: You are not registered as a seller.[/red]")
        console.print("[yellow]Register as a seller: zanchop register --role seller[/yellow]")
        ctx.exit(1)

@seller.command()
@click.option('--name', prompt='Product name', help='Name of the food item')
@click.option('--price', prompt='Price (GHS)', type=float, help='Price in GHS')
@click.option('--stock', default=1, help='Quantity available')
@click.option('--description', default='', help='Description of the item')
@click.pass_context
def add_product(ctx, name, price, stock, description):
    """Add a new food item to your menu."""
    phone = ctx.obj.phone
    
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO products (seller_phone, name, description, price, stock) VALUES (?, ?, ?, ?, ?)",
        (phone, name, description, price, stock)
    )
    conn.commit()
    conn.close()
    
    console.print(f"[green]✅ Added '{name}' to your menu - GHS {price:.2f}[/green]")

@seller.command(name='my-products')
@click.pass_context
def my_products(ctx):
    """View all your food items."""
    phone = ctx.obj.phone
    
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT id, name, price, stock FROM products WHERE seller_phone = ?",
        (phone,)
    )
    products = c.fetchall()
    conn.close()
    
    if not products:
        console.print("[yellow]📭 Your menu is empty.[/yellow]")
        console.print("[cyan]Add items: zanchop --phone {phone} seller add-product[/cyan]".format(phone=phone))
        return
    
    table = Table(title="🍽️ Your Menu", show_header=True, header_style="bold magenta")
    table.add_column("ID", style="dim", justify="right")
    table.add_column("Item Name", style="cyan")
    table.add_column("Price (GHS)", justify="right", style="green")
    table.add_column("Stock", justify="right", style="yellow")
    
    for p in products:
        table.add_row(str(p['id']), p['name'], f"{p['price']:.2f}", str(p['stock']))
    
    console.print(table)

@seller.command(name='my-orders')
@click.option('--status', default='pending', type=click.Choice(['pending', 'all']), help='Filter by status')
@click.pass_context
def my_orders(ctx, status):
    """View orders received from buyers."""
    phone = ctx.obj.phone
    
    conn = get_db()
    c = conn.cursor()
    
    if status == 'pending':
        c.execute(
            "SELECT id, buyer_phone, total_price, status, created_at FROM orders WHERE seller_phone = ? AND status = 'pending' ORDER BY created_at DESC",
            (phone,)
        )
    else:
        c.execute(
            "SELECT id, buyer_phone, total_price, status, created_at FROM orders WHERE seller_phone = ? ORDER BY created_at DESC LIMIT 20",
            (phone,)
        )
    
    orders = c.fetchall()
    conn.close()
    
    if not orders:
        console.print("[yellow]📭 No orders found.[/yellow]")
        return
    
    table = Table(title=f"📦 Your Orders ({status.upper()})", show_header=True, header_style="bold magenta")
    table.add_column("Order #", style="dim", justify="right")
    table.add_column("Buyer", style="cyan")
    table.add_column("Total (GHS)", justify="right", style="green")
    table.add_column("Status", style="yellow")
    table.add_column("Date", style="dim")
    
    for o in orders:
        table.add_row(
            str(o['id']),
            o['buyer_phone'],
            f"{o['total_price']:.2f}",
            o['status'].upper(),
            o['created_at'][:10] if o['created_at'] else ''
        )
    
    console.print(table)

# =========================
# BUYER COMMANDS
# =========================
@cli.group()
@click.pass_context
def buyer(ctx):
    """Buyer commands - Browse and order food."""
    ctx.ensure_object(UserContext)
    if not ctx.obj.phone:
        console.print("[red]❌ Error: Please provide your phone number with --phone[/red]")
        console.print("[yellow]Example: zanchop --phone 233xxxxxxxxx buyer browse[/yellow]")
        ctx.exit(1)
    
    # Verify user is a buyer
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT role FROM users WHERE phone = ?", (ctx.obj.phone,))
    user = c.fetchone()
    conn.close()
    
    if not user:
        console.print("[red]❌ Error: User not found. Please register first.[/red]")
        ctx.exit(1)
    
    if user['role'] != 'buyer':
        console.print("[red]❌ Error: You are not registered as a buyer.[/red]")
        console.print("[yellow]Register as a buyer: zanchop register --role buyer[/yellow]")
        ctx.exit(1)

@buyer.command()
@click.pass_context
def browse(ctx):
    """Browse available food items."""
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """SELECT p.id, p.name, p.price, p.stock, u.name as seller_name, u.zone 
           FROM products p 
           JOIN users u ON p.seller_phone = u.phone 
           WHERE p.stock > 0 
           ORDER BY p.id DESC"""
    )
    products = c.fetchall()
    conn.close()
    
    if not products:
        console.print("[yellow]😔 No food available currently.[/yellow]")
        console.print("[cyan]Check back later or encourage your friends to sell food![/cyan]")
        return
    
    table = Table(title="🍴 Available Food", show_header=True, header_style="bold magenta")
    table.add_column("ID", style="dim", justify="right")
    table.add_column("Item", style="cyan")
    table.add_column("Seller", style="blue")
    table.add_column("Zone", style="dim")
    table.add_column("Price (GHS)", justify="right", style="green")
    table.add_column("Available", justify="right", style="yellow")
    
    for p in products:
        table.add_row(
            str(p['id']),
            p['name'],
            p['seller_name'],
            p['zone'],
            f"{p['price']:.2f}",
            str(p['stock'])
        )
    
    console.print(table)
    console.print("\n[cyan]To order: zanchop --phone {phone} buyer order --product-id <ID> --qty <quantity>[/cyan]".format(phone=ctx.obj.phone))

@buyer.command()
@click.option('--product-id', prompt='Product ID', type=int, help='ID of the product to order')
@click.option('--qty', prompt='Quantity', type=int, default=1, help='How many to order')
@click.pass_context
def order(ctx, product_id, qty):
    """Place an order for food."""
    phone = ctx.obj.phone
    
    conn = get_db()
    c = conn.cursor()
    
    # Get product info
    c.execute(
        "SELECT seller_phone, name, price, stock FROM products WHERE id = ?",
        (product_id,)
    )
    product = c.fetchone()
    
    if not product:
        console.print("[red]❌ Error: Product not found.[/red]")
        conn.close()
        return
    
    if product['stock'] < qty:
        console.print(f"[red]❌ Error: Only {product['stock']} items available.[/red]")
        conn.close()
        return
    
    # Get buyer zone for delivery fee
    c.execute("SELECT zone FROM users WHERE phone = ?", (phone,))
    buyer = c.fetchone()
    buyer_zone = buyer['zone'] if buyer else 'North Campus (Casford/KNH)'
    delivery_fee = UCC_ZONES.get(buyer_zone, 5.0)
    
    # Calculate totals
    food_total = product['price'] * qty
    grand_total = food_total + delivery_fee
    
    # Show order summary
    console.print(Panel(
        f"[bold cyan]{product['name']}[/bold cyan] x {qty}\n"
        f"Subtotal: GHS {food_total:.2f}\n"
        f"Delivery ({buyer_zone}): GHS {delivery_fee:.2f}\n"
        f"[bold green]Total: GHS {grand_total:.2f}[/bold green]",
        title="📋 Order Summary",
        border_style="cyan"
    ))
    
    if click.confirm("Confirm order?"):
        # Create order
        c.execute(
            """INSERT INTO orders (buyer_phone, seller_phone, total_price, delivery_fee, status) 
               VALUES (?, ?, ?, ?, 'pending')""",
            (phone, product['seller_phone'], grand_total, delivery_fee)
        )
        order_id = c.lastrowid
        
        # Add order item
        c.execute(
            "INSERT INTO order_items (order_id, product_id, quantity, price_at_purchase) VALUES (?, ?, ?, ?)",
            (order_id, product_id, qty, product['price'])
        )
        
        # Update stock
        c.execute(
            "UPDATE products SET stock = stock - ? WHERE id = ?",
            (qty, product_id)
        )
        
        conn.commit()
        conn.close()
        
        console.print(f"[green]✅ Order #{order_id} placed successfully![/green]")
        console.print(f"[cyan]The seller has been notified.[/cyan]")
    else:
        conn.close()
        console.print("[yellow]Order cancelled.[/yellow]")

@buyer.command(name='my-orders')
@click.pass_context
def buyer_orders(ctx):
    """View your order history."""
    phone = ctx.obj.phone
    
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """SELECT o.id, p.name, o.total_price, o.status, o.created_at 
           FROM orders o 
           JOIN order_items oi ON o.id = oi.order_id 
           JOIN products p ON oi.product_id = p.id 
           WHERE o.buyer_phone = ? 
           ORDER BY o.created_at DESC LIMIT 10""",
        (phone,)
    )
    orders = c.fetchall()
    conn.close()
    
    if not orders:
        console.print("[yellow]📭 You haven't placed any orders yet.[/yellow]")
        console.print("[cyan]Browse food: zanchop --phone {phone} buyer browse[/cyan]".format(phone=phone))
        return
    
    table = Table(title="🛒 Your Orders", show_header=True, header_style="bold magenta")
    table.add_column("Order #", style="dim", justify="right")
    table.add_column("Item", style="cyan")
    table.add_column("Total (GHS)", justify="right", style="green")
    table.add_column("Status", style="yellow")
    table.add_column("Date", style="dim")
    
    for o in orders:
        table.add_row(
            str(o['id']),
            o['name'],
            f"{o['total_price']:.2f}",
            o['status'].upper(),
            o['created_at'][:10] if o['created_at'] else ''
        )
    
    console.print(table)

# =========================
# PROFILE COMMANDS
# =========================
@cli.group()
def profile():
    """Manage your profile."""
    pass

@profile.command()
@click.option('--phone', required=True, help='Your phone number')
@click.option('--name', help='Update your name')
@click.option('--zone', type=click.Choice(list(UCC_ZONES.keys())), help='Update your zone')
def update(phone, name, zone):
    """Update your profile information."""
    if not name and not zone:
        console.print("[yellow]⚠️ Nothing to update. Use --name or --zone options.[/yellow]")
        return
    
    conn = get_db()
    c = conn.cursor()
    
    # Check user exists
    c.execute("SELECT phone FROM users WHERE phone = ?", (phone,))
    if not c.fetchone():
        console.print("[red]❌ User not found.[/red]")
        conn.close()
        return
    
    if name:
        c.execute("UPDATE users SET name = ? WHERE phone = ?", (name, phone))
        console.print(f"[green]✅ Name updated to: {name}[/green]")
    
    if zone:
        c.execute("UPDATE users SET zone = ? WHERE phone = ?", (zone, phone))
        console.print(f"[green]✅ Zone updated to: {zone}[/green]")
    
    conn.commit()
    conn.close()

@profile.command()
@click.option('--phone', required=True, help='Your phone number')
def view(phone):
    """View your profile details."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE phone = ?", (phone,))
    user = c.fetchone()
    conn.close()
    
    if not user:
        console.print("[red]❌ User not found.[/red]")
        return
    
    console.print(Panel(
        f"[bold]Name:[/bold] {user['name']}\n"
        f"[bold]Phone:[/bold] {user['phone']}\n"
        f"[bold]Role:[/bold] {user['role'].capitalize()}\n"
        f"[bold]Zone:[/bold] {user['zone']}\n"
        f"[bold]Joined:[/bold] {user['created_at'][:10] if user['created_at'] else 'N/A'}",
        title="👤 Your Profile",
        border_style="blue"
    ))

# =========================
# ADMIN COMMANDS
# =========================
ADMIN_PHONE = os.getenv("ADMIN_PHONE", "")

@cli.group()
@click.pass_context
def admin(ctx):
    """Admin commands - Manage the marketplace."""
    ctx.ensure_object(UserContext)
    if not ctx.obj.phone:
        console.print("[red]❌ Error: Please provide your phone number with --phone[/red]")
        ctx.exit(1)
    
    # Verify admin
    if ctx.obj.phone != ADMIN_PHONE:
        console.print("[red]❌ Error: Unauthorized. Admin access only.[/red]")
        ctx.exit(1)

@admin.command(name='users')
@click.pass_context
def admin_users(ctx):
    """List all registered users."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT phone, name, role, zone, created_at FROM users ORDER BY created_at DESC")
    users = c.fetchall()
    conn.close()
    
    if not users:
        console.print("[yellow]No users found.[/yellow]")
        return
    
    table = Table(title="👥 All Users", show_header=True, header_style="bold red")
    table.add_column("Phone", style="cyan")
    table.add_column("Name", style="green")
    table.add_column("Role", style="yellow")
    table.add_column("Zone", style="dim")
    table.add_column("Joined", style="dim")
    
    for u in users:
        table.add_row(
            u['phone'],
            u['name'],
            u['role'].upper(),
            u['zone'],
            u['created_at'][:10] if u['created_at'] else ''
        )
    
    console.print(table)

@admin.command(name='products')
@click.pass_context
def admin_products(ctx):
    """List all products in the marketplace."""
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """SELECT p.id, p.name, p.price, p.stock, u.name as seller_name, p.seller_phone 
           FROM products p 
           JOIN users u ON p.seller_phone = u.phone 
           ORDER BY p.id DESC"""
    )
    products = c.fetchall()
    conn.close()
    
    if not products:
        console.print("[yellow]No products found.[/yellow]")
        return
    
    table = Table(title="🍽️ All Products", show_header=True, header_style="bold red")
    table.add_column("ID", style="dim", justify="right")
    table.add_column("Item", style="cyan")
    table.add_column("Seller", style="green")
    table.add_column("Phone", style="dim")
    table.add_column("Price (GHS)", justify="right", style="yellow")
    table.add_column("Stock", justify="right", style="blue")
    
    for p in products:
        table.add_row(
            str(p['id']),
            p['name'],
            p['seller_name'],
            p['seller_phone'],
            f"{p['price']:.2f}",
            str(p['stock'])
        )
    
    console.print(table)

@admin.command(name='orders')
@click.pass_context
def admin_orders(ctx):
    """List all orders in the system."""
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """SELECT o.id, o.buyer_phone, o.seller_phone, o.total_price, o.status, o.created_at,
                  p.name as product_name
           FROM orders o 
           JOIN order_items oi ON o.id = oi.order_id 
           JOIN products p ON oi.product_id = p.id 
           ORDER BY o.created_at DESC LIMIT 50"""
    )
    orders = c.fetchall()
    conn.close()
    
    if not orders:
        console.print("[yellow]No orders found.[/yellow]")
        return
    
    table = Table(title="📦 All Orders", show_header=True, header_style="bold red")
    table.add_column("Order #", style="dim", justify="right")
    table.add_column("Item", style="cyan")
    table.add_column("Buyer", style="green")
    table.add_column("Seller", style="blue")
    table.add_column("Total", justify="right", style="yellow")
    table.add_column("Status", style="magenta")
    table.add_column("Date", style="dim")
    
    for o in orders:
        table.add_row(
            str(o['id']),
            o['product_name'],
            o['buyer_phone'],
            o['seller_phone'],
            f"{o['total_price']:.2f}",
            o['status'].upper(),
            o['created_at'][:10] if o['created_at'] else ''
        )
    
    console.print(table)

@admin.command(name='stats')
@click.pass_context
def admin_stats(ctx):
    """Show marketplace statistics."""
    conn = get_db()
    c = conn.cursor()
    
    # Count users by role
    c.execute("SELECT role, COUNT(*) as count FROM users GROUP BY role")
    user_counts = c.fetchall()
    
    # Count products
    c.execute("SELECT COUNT(*) as count FROM products")
    product_count = c.fetchone()['count']
    
    # Count orders
    c.execute("SELECT COUNT(*) as count FROM orders")
    order_count = c.fetchone()['count']
    
    # Total revenue
    c.execute("SELECT SUM(total_price) as total FROM orders WHERE status = 'pending'")
    revenue = c.fetchone()['total'] or 0
    
    conn.close()
    
    console.print(Panel(
        f"[bold cyan]Users:[/bold cyan]\n" +
        "\n".join([f"  {uc['role'].capitalize()}: {uc['count']}" for uc in user_counts]) +
        f"\n\n[bold cyan]Products:[/bold cyan] {product_count}\n"
        f"[bold cyan]Orders:[/bold cyan] {order_count}\n"
        f"[bold green]Total Revenue: GHS {revenue:.2f}[/bold green]",
        title="📊 Marketplace Stats",
        border_style="red"
    ))

# =========================
# MAIN
# =========================
if __name__ == '__main__':
    cli()